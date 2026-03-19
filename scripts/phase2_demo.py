"""
Phase 2 demo: Domain logic (aggregates, commands, business rules) with seeded data.

Assumes the event store is already migrated and seeded from starter/data/seed_events.jsonl.
Uses a loan application that has CreditAnalysisRequested in the seed (e.g. APEX-0012)
so the LoanApplication aggregate reconstructs to state AWAITING_ANALYSIS.

Flow:
  1. Load LoanApplication aggregate for the chosen application → expect AWAITING_ANALYSIS.
  2. Start an agent session (Gas Town: AgentContextLoaded).
  3. Run handle_credit_analysis_completed → append CreditAnalysisCompleted to loan stream.
  4. Re-load loan aggregate → expect ANALYSIS_COMPLETE.

Usage:
  uv run python scripts/phase2_demo.py
  uv run python scripts/phase2_demo.py --application-id APEX-0014
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Wire upcasters before importing EventStore/aggregates that use StoredEvent
import src.upcasting.upcasters  # noqa: F401
from src.event_store import set_registry
from src.upcasting.registry import registry

set_registry(registry)

from src.aggregates.loan_application import LoanApplicationAggregate
from src.commands.handlers import (
    CreditAnalysisCompletedCommand,
    StartAgentSessionCommand,
    handle_credit_analysis_completed,
    handle_start_agent_session,
)
from src.db.pool import create_pool
from src.event_store import EventStore
from src.models.events import ApplicationState


AGENT_ID = "phase2-demo-agent"
SESSION_ID = "phase2-demo-session"
MODEL_VERSION = "claude-sonnet-4-20250514"


async def run_demo(application_id: str) -> None:
    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql://ledger:ledger_dev@localhost:5432/ledger",
    )
    pool = await create_pool(dsn)
    store = EventStore(pool)
    try:
        # 1. Reconstruct loan aggregate from seed
        print(f"[1] Loading LoanApplication aggregate for {application_id}...")
        app = await LoanApplicationAggregate.load(store, application_id)
        print(f"    State: {app.state}, Version: {app.version}")

        if app.state != ApplicationState.AWAITING_ANALYSIS:
            print(
                f"    ERROR: Expected state AWAITING_ANALYSIS (seed must include CreditAnalysisRequested for this application)."
            )
            print("    Use an application ID that has CreditAnalysisRequested in seed, e.g. APEX-0012, APEX-0013, APEX-0014.")
            sys.exit(1)

        # 2. Start agent session (Gas Town)
        print(f"[2] Starting agent session {AGENT_ID} / {SESSION_ID}...")
        await handle_start_agent_session(
            StartAgentSessionCommand(
                agent_id=AGENT_ID,
                session_id=SESSION_ID,
                context_source="phase2-demo",
                model_version=MODEL_VERSION,
                context_token_count=1500,
            ),
            store,
        )
        print("    AgentContextLoaded appended.")

        # 3. Credit analysis completed
        print(f"[3] Appending CreditAnalysisCompleted for {application_id}...")
        new_version = await handle_credit_analysis_completed(
            CreditAnalysisCompletedCommand(
                application_id=application_id,
                agent_id=AGENT_ID,
                session_id=SESSION_ID,
                model_version=MODEL_VERSION,
                confidence_score=0.82,
                risk_tier="LOW",
                recommended_limit_usd=1_200_000.0,
                duration_ms=2100,
                input_data={"application_id": application_id},
            ),
            store,
        )
        print(f"    Appended; loan stream version: {new_version}.")

        # 4. Re-load and verify
        print("[4] Re-loading LoanApplication aggregate...")
        app2 = await LoanApplicationAggregate.load(store, application_id)
        print(f"    State: {app2.state}, Version: {app2.version}")

        if app2.state != ApplicationState.ANALYSIS_COMPLETE:
            print(f"    ERROR: Expected state ANALYSIS_COMPLETE, got {app2.state}")
            sys.exit(1)

        print("")
        print("Phase 2 demo completed successfully.")
        print("  - Aggregate state reconstructed from event history (load → apply).")
        print("  - Business rules enforced in command handler (BR-1, BR-2, BR-3).")
        print("  - New event appended with expected_version (optimistic concurrency).")
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Phase 2 domain logic demo against seeded event store."
    )
    parser.add_argument(
        "--application-id",
        default="APEX-0012",
        help="Loan application ID (must have CreditAnalysisRequested in seed; default: APEX-0012)",
    )
    args = parser.parse_args()
    asyncio.run(run_demo(args.application_id))


if __name__ == "__main__":
    main()
