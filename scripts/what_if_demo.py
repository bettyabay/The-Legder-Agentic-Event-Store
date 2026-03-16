"""
What-If Demo — Counterfactual Scenario Analysis (Phase 6 Bonus).

Demonstrates the what-if projector by comparing a real loan application outcome
against a counterfactual scenario where the credit analysis used a different
risk tier (e.g., HIGH instead of MEDIUM).

This lets compliance teams answer: "What would the AI have decided if the
credit agent had flagged this application as HIGH risk instead of MEDIUM?"

Usage:
  uv run python scripts/what_if_demo.py --application-id demo-001
  uv run python scripts/what_if_demo.py  (seeds a new application and runs what-if on it)
  uv run python scripts/what_if_demo.py --application-id demo-001 --risk-tier HIGH
  uv run python scripts/what_if_demo.py --application-id demo-001 --confidence 0.45
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg

from src.db.pool import create_pool, run_migrations
from src.event_store import EventStore
from src.models.events import CreditAnalysisCompleted
from src.what_if.projector import WhatIfResult, run_what_if

# Wire upcasters
import src.upcasting.upcasters  # noqa: F401
from src.event_store import set_registry
from src.upcasting.registry import registry

set_registry(registry)


def _print_outcome(label: str, outcome: dict) -> None:
    print(f"  {label}:")
    print(f"    Final State         : {outcome.get('final_state', 'N/A')}")
    print(f"    Risk Tier           : {outcome.get('risk_tier', 'N/A')}")
    if outcome.get("recommended_limit_usd") is not None:
        print(f"    Recommended Limit   : ${outcome['recommended_limit_usd']:,.0f}")
    print(f"    Decision            : {outcome.get('decision', 'N/A')}")
    if outcome.get("confidence_score") is not None:
        print(f"    Confidence Score    : {outcome['confidence_score']:.2f}")
    print(f"    Events Replayed     : {len(outcome.get('events', []))}")


def _print_result(result: WhatIfResult) -> None:
    print(f"\n{'='*64}")
    print(f"What-If Analysis: {result.application_id}")
    print(f"Branch Point    : {result.branch_at_event_type}")
    print(f"{'='*64}")

    _print_outcome("REAL outcome    ", result.real_outcome)
    print()
    _print_outcome("COUNTERFACTUAL  ", result.counterfactual_outcome)

    print(f"\n  Events replayed (real)          : {result.events_replayed_real}")
    print(f"  Events replayed (counterfactual): {result.events_replayed_counterfactual}")

    if result.divergence_events:
        print(f"\n  ⚡ Divergence detected ({len(result.divergence_events)} field(s)):")
        for div in result.divergence_events:
            field = div["field"]
            real_val = div["real"]
            cf_val = div["counterfactual"]
            print(f"    {field:25s}: real={real_val!r:20s} → counterfactual={cf_val!r}")
    else:
        print(f"\n  ✓ No divergence — counterfactual produces the same outcome as real")

    print(f"{'='*64}\n")


async def _ensure_application(
    pool: asyncpg.Pool,
    application_id: str,
    store: EventStore,
) -> bool:
    """Return True if application exists with at least a CreditAnalysisCompleted event."""
    try:
        events = await store.load_stream(f"loan-{application_id}")
        return any(e.event_type == "CreditAnalysisCompleted" for e in events)
    except Exception:
        return False


async def run_what_if_demo(
    pool: asyncpg.Pool,
    application_id: str,
    counterfactual_risk_tier: str = "HIGH",
    counterfactual_confidence: float | None = None,
) -> None:
    store = EventStore(pool=pool)

    # Check if application has a CreditAnalysisCompleted event to branch at
    has_credit = await _ensure_application(pool, application_id, store)
    if not has_credit:
        print(f"\n⚠ Application '{application_id}' has no CreditAnalysisCompleted event.")
        print(
            "  Run: uv run python scripts/seed_demo_application.py "
            f"--application-id {application_id}"
        )
        return

    # Load the original credit analysis to carry forward unchanged fields
    events = await store.load_stream(f"loan-{application_id}")
    original_credit = next(
        (e for e in events if e.event_type == "CreditAnalysisCompleted"), None
    )
    if original_credit is None:
        print("⚠ No CreditAnalysisCompleted found in stream.")
        return

    op = original_credit.payload
    cf_confidence = counterfactual_confidence if counterfactual_confidence is not None else (
        op.get("confidence_score") or 0.75
    )

    # Build the counterfactual event (same fields, different risk_tier / confidence)
    counterfactual_event = CreditAnalysisCompleted(
        application_id=op["application_id"],
        agent_id=op["agent_id"],
        session_id=op.get("session_id", "counterfactual-session"),
        model_version=op.get("model_version", "cf-model"),
        confidence_score=cf_confidence,
        risk_tier=counterfactual_risk_tier,
        recommended_limit_usd=op.get("recommended_limit_usd", 500_000.0) * (
            0.5 if counterfactual_risk_tier == "HIGH" else 1.0
        ),
        analysis_duration_ms=op.get("analysis_duration_ms", 1000),
        input_data_hash=op.get("input_data_hash", "counterfactual"),
    )

    print(f"\n📊 Counterfactual Setup:")
    print(f"   Real credit risk tier     : {op.get('risk_tier')}")
    print(f"   Counterfactual risk tier  : {counterfactual_risk_tier}")
    print(f"   Real confidence score     : {op.get('confidence_score')}")
    print(f"   Counterfactual confidence : {cf_confidence:.2f}")

    t = time.monotonic()
    result = await run_what_if(
        store=store,
        application_id=application_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=[counterfactual_event],
    )
    elapsed_ms = (time.monotonic() - t) * 1000

    _print_result(result)
    print(f"  What-if computation time: {elapsed_ms:.1f}ms")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a what-if counterfactual analysis on a seeded application."
    )
    parser.add_argument(
        "--application-id",
        default=None,
        help="Application ID to analyse (default: seeds a new demo application)",
    )
    parser.add_argument(
        "--risk-tier",
        default="HIGH",
        choices=["LOW", "MEDIUM", "HIGH"],
        help="Counterfactual risk tier to inject (default: HIGH)",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=None,
        help="Counterfactual confidence score (default: same as original)",
    )
    args = parser.parse_args()

    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql://ledger:ledger_dev@localhost:5432/ledger",
    )
    pool = await create_pool(dsn)
    await run_migrations(pool)

    application_id = args.application_id

    if application_id is None:
        # Auto-seed a fresh application for demonstration
        from scripts.seed_demo_application import seed_application

        application_id = f"whatif-{uuid4().hex[:10]}"
        print(f"🌱 Seeding fresh application: {application_id}")
        await seed_application(pool, application_id)

    try:
        await run_what_if_demo(
            pool,
            application_id=application_id,
            counterfactual_risk_tier=args.risk_tier,
            counterfactual_confidence=args.confidence,
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
