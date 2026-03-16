"""
Phase 4A Mandatory Test: Upcasting Immutability Test

Verifies that upcasting transforms events at read time WITHOUT modifying the
stored payload. The raw database row must be byte-for-byte identical before
and after any load_stream() call.

Run: uv run pytest tests/test_upcasting.py -v
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest

from src.event_store import EventStore, set_registry
from src.upcasting.registry import registry

# Wire the registry before tests run
import src.upcasting.upcasters  # noqa: F401 — side-effect: registers upcasters


@pytest.fixture(autouse=True)
def _wire_registry() -> None:
    """Ensure the global registry is wired into event_store before each test."""
    set_registry(registry)


@pytest.mark.asyncio
async def test_v1_event_loaded_as_v2_via_upcaster(
    db_pool: asyncpg.Pool,
) -> None:
    """A v1 stored event is returned as v2 by load_stream() without DB mutation.

    Assessment step (challenge spec):
      1. Directly INSERT a v1 CreditAnalysisCompleted into events table
      2. Query raw DB → raw_before
      3. load_stream() → assert returned event is v2 with new fields
      4. Query raw DB again → assert payload == raw_before
    """
    stream_id = f"loan-upcast-{uuid4()}"

    # ── Step 0: Create the stream ─────────────────────────────────────────
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO event_streams (stream_id, aggregate_type, current_version) "
            "VALUES ($1, 'loan', 1)",
            stream_id,
        )

    # ── Step 1: Directly INSERT a v1 event ───────────────────────────────
    v1_payload = {
        "application_id": "app-upcast-test",
        "agent_id": "agent-upcast-001",
        "session_id": "session-upcast-001",
        # NOTE: no model_version, confidence_score, or regulatory_basis (v1 schema)
        "risk_tier": "MEDIUM",
        "recommended_limit_usd": 300_000.0,
        "analysis_duration_ms": 1500,
        "input_data_hash": "hash-v1",
    }
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO events "
            "(stream_id, stream_position, event_type, event_version, payload, metadata) "
            "VALUES ($1, 1, 'CreditAnalysisCompleted', 1, $2::jsonb, '{}'::jsonb) "
            "RETURNING event_id",
            stream_id,
            json.dumps(v1_payload),
        )
    event_id = row["event_id"]

    # ── Step 2: Query raw payload from DB ─────────────────────────────────
    async with db_pool.acquire() as conn:
        raw_row = await conn.fetchrow(
            "SELECT payload, event_version FROM events WHERE event_id = $1",
            event_id,
        )
    raw_before = raw_row["payload"]
    assert raw_row["event_version"] == 1, "Stored event_version must be 1"

    # ── Step 3: Load via EventStore (upcasting applied at read time) ──────
    store = EventStore(pool=db_pool)
    events = await store.load_stream(stream_id)

    assert len(events) == 1, f"Expected 1 event in stream, got {len(events)}"
    loaded_event = events[0]

    # Upcast must produce v2
    assert loaded_event.event_version == 2, (
        f"Expected event_version=2 after upcast, got {loaded_event.event_version}"
    )
    assert "model_version" in loaded_event.payload, (
        "Upcasted event must have 'model_version' field"
    )
    assert "confidence_score" in loaded_event.payload, (
        "Upcasted event must have 'confidence_score' field"
    )

    # v1 events get model_version="legacy-pre-2026"
    assert loaded_event.payload["model_version"] == "legacy-pre-2026", (
        f"Expected 'legacy-pre-2026', got {loaded_event.payload['model_version']}"
    )

    # confidence_score should be None (genuinely unknown, not fabricated)
    assert loaded_event.payload["confidence_score"] is None, (
        f"Expected None for unknown confidence_score, got {loaded_event.payload['confidence_score']}"
    )

    # ── Step 4: Query raw DB AGAIN — must be unchanged ────────────────────
    async with db_pool.acquire() as conn:
        raw_row_after = await conn.fetchrow(
            "SELECT payload, event_version FROM events WHERE event_id = $1",
            event_id,
        )
    raw_after = raw_row_after["payload"]

    # Payload must be byte-for-byte unchanged
    raw_before_dict = raw_before if isinstance(raw_before, dict) else json.loads(raw_before)
    raw_after_dict = raw_after if isinstance(raw_after, dict) else json.loads(raw_after)

    assert raw_before_dict == raw_after_dict, (
        "Stored payload was MUTATED by upcasting — this breaks event sourcing immutability!\n"
        f"Before: {raw_before_dict}\n"
        f"After:  {raw_after_dict}"
    )

    # event_version in DB must still be 1
    assert raw_row_after["event_version"] == 1, (
        f"event_version in DB was modified from 1 to {raw_row_after['event_version']}"
    )


@pytest.mark.asyncio
async def test_raw_stored_payload_unchanged_after_upcast(
    db_pool: asyncpg.Pool,
) -> None:
    """The stored event payload in the database is never mutated by any load call."""
    # A second guard test using DecisionGenerated v1→v2 upcaster
    stream_id = f"loan-decision-{uuid4()}"

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO event_streams (stream_id, aggregate_type, current_version) "
            "VALUES ($1, 'loan', 1)",
            stream_id,
        )

    v1_payload = {
        "application_id": "app-decision-001",
        "orchestrator_agent_id": "agent-orch-001",
        "recommendation": "APPROVE",
        "confidence_score": 0.85,
        "contributing_agent_sessions": ["agent-credit-001-session-001"],
        "decision_basis_summary": "All analyses passed.",
        # No model_versions dict (v1 schema)
    }

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO events "
            "(stream_id, stream_position, event_type, event_version, payload, metadata) "
            "VALUES ($1, 1, 'DecisionGenerated', 1, $2::jsonb, '{}'::jsonb) "
            "RETURNING event_id",
            stream_id,
            json.dumps(v1_payload),
        )
    event_id = row["event_id"]

    async with db_pool.acquire() as conn:
        before = await conn.fetchrow(
            "SELECT payload, event_version FROM events WHERE event_id = $1", event_id
        )

    store = EventStore(pool=db_pool)
    events = await store.load_stream(stream_id)
    assert len(events) == 1

    # Loaded event should be v2
    assert events[0].event_version == 2
    assert "model_versions" in events[0].payload

    async with db_pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT payload, event_version FROM events WHERE event_id = $1", event_id
        )

    before_dict = before["payload"] if isinstance(before["payload"], dict) else json.loads(before["payload"])
    after_dict = after["payload"] if isinstance(after["payload"], dict) else json.loads(after["payload"])

    assert before_dict == after_dict, "Stored payload mutated!"
    assert after["event_version"] == 1, "event_version in DB was modified!"
