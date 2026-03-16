"""
Phase 1 Mandatory Test: Double-Decision Concurrency Test

Tests that exactly one of two concurrent asyncio tasks can append to a stream
at the same expected_version. The other MUST receive OptimisticConcurrencyError.

Assessment criterion (Score 3 gate):
  (a) total events appended to the stream = 4 (not 5)
  (b) the winning task's event has stream_position = 4
  (c) the losing task's OptimisticConcurrencyError is raised, not silently swallowed

Run: uv run pytest tests/test_concurrency.py -v
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import asyncpg
import pytest

from src.db.pool import create_pool, run_migrations
from src.event_store import EventStore
from src.models.events import ApplicationSubmitted, CreditAnalysisCompleted
from src.models.exceptions import OptimisticConcurrencyError


@pytest.mark.asyncio
async def test_double_decision_exactly_one_wins(
    db_pool: asyncpg.Pool,
    unique_app_id: str,
) -> None:
    """Two concurrent agents appending at expected_version=3: exactly one wins.

    Scenario (Apex Financial):
      Two fraud-detection agents simultaneously flag the same application.
      Without OCC both flags are applied creating inconsistent state.
      With OCC one agent's decision wins; the other must reload and retry.
    """
    store = EventStore(pool=db_pool)
    now = datetime.now(tz=timezone.utc)

    # ── Seed the stream to version 3 (3 events) ──────────────────────────
    seed_events = [
        ApplicationSubmitted(
            application_id=unique_app_id,
            applicant_id="applicant-001",
            requested_amount_usd=500_000.00,
            loan_purpose="Commercial expansion",
            submission_channel="api",
            submitted_at=now,
        ),
        CreditAnalysisCompleted(
            application_id=unique_app_id,
            agent_id="agent-credit-001",
            session_id="session-001",
            model_version="v2.3",
            confidence_score=0.82,
            risk_tier="MEDIUM",
            recommended_limit_usd=400_000.00,
            analysis_duration_ms=1200,
            input_data_hash="abc123",
        ),
        CreditAnalysisCompleted(
            application_id=unique_app_id,
            agent_id="agent-credit-002",
            session_id="session-002",
            model_version="v2.3",
            confidence_score=0.77,
            risk_tier="MEDIUM",
            recommended_limit_usd=380_000.00,
            analysis_duration_ms=1350,
            input_data_hash="def456",
        ),
    ]
    stream_id = f"loan-{unique_app_id}"
    await store.append(stream_id, seed_events, expected_version=-1)

    # Confirm we're at version 3
    v = await store.stream_version(stream_id)
    assert v == 3, f"Expected stream version 3 after seeding, got {v}"

    # ── Conflicting event — both tasks try to append at version 3 ────────
    conflict_event = CreditAnalysisCompleted(
        application_id=unique_app_id,
        agent_id="agent-fraud-001",
        session_id="session-conflict",
        model_version="v2.4",
        confidence_score=0.65,
        risk_tier="HIGH",
        recommended_limit_usd=200_000.00,
        analysis_duration_ms=900,
        input_data_hash="conflict-hash",
    )

    results: list[Exception | int] = []

    async def attempt_append() -> None:
        try:
            new_version = await store.append(
                stream_id, [conflict_event], expected_version=3
            )
            results.append(new_version)
        except OptimisticConcurrencyError as exc:
            results.append(exc)

    # ── Launch both tasks concurrently ────────────────────────────────────
    await asyncio.gather(attempt_append(), attempt_append())

    # ── Assertions ────────────────────────────────────────────────────────
    successes = [r for r in results if isinstance(r, int)]
    failures = [r for r in results if isinstance(r, OptimisticConcurrencyError)]

    # (a) Exactly one success, exactly one failure
    assert len(successes) == 1, (
        f"Expected exactly 1 successful append, got {len(successes)}. Results: {results}"
    )
    assert len(failures) == 1, (
        f"Expected exactly 1 OptimisticConcurrencyError, got {len(failures)}. Results: {results}"
    )

    # (b) Winning task's event has stream_position = 4
    assert successes[0] == 4, (
        f"Expected winning version = 4, got {successes[0]}"
    )

    # (c) Total events in stream = 4 (not 5)
    events = await store.load_stream(stream_id)
    assert len(events) == 4, (
        f"Expected 4 total events in stream, got {len(events)}"
    )

    # (d) Last event is at stream_position 4
    assert events[-1].stream_position == 4


@pytest.mark.asyncio
async def test_concurrency_error_is_not_silently_swallowed(
    db_pool: asyncpg.Pool,
    unique_app_id: str,
) -> None:
    """OptimisticConcurrencyError propagates as a proper exception, not silently caught."""
    store = EventStore(pool=db_pool)
    stream_id = f"loan-{unique_app_id}"

    # Create a stream with 1 event
    event = ApplicationSubmitted(
        application_id=unique_app_id,
        applicant_id="applicant-002",
        requested_amount_usd=100_000.00,
        loan_purpose="Working capital",
        submission_channel="web",
        submitted_at=datetime.now(tz=timezone.utc),
    )
    await store.append(stream_id, [event], expected_version=-1)

    # Attempt to append at version 0 (stream is now at version 1)
    dummy_event = ApplicationSubmitted(
        application_id=unique_app_id,
        applicant_id="applicant-002b",
        requested_amount_usd=200_000.00,
        loan_purpose="Duplicate attempt",
        submission_channel="web",
        submitted_at=datetime.now(tz=timezone.utc),
    )
    with pytest.raises(OptimisticConcurrencyError) as exc_info:
        await store.append(stream_id, [dummy_event], expected_version=0)

    err = exc_info.value
    assert err.stream_id == stream_id
    assert err.expected_version == 0
    assert err.actual_version == 1
    assert err.suggested_action == "reload_stream_and_retry"
