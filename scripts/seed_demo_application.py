"""
Seed Demo Application — Full Lifecycle via MCP Tool Calls.

Drives a complete Apex Financial Services loan application from submission
through final human review using ONLY MCP tool dispatches.

Lifecycle driven:
  start_agent_session × 3 agents (credit, fraud, orchestrator)
  submit_application
  record_credit_analysis
  record_fraud_screening
  record_compliance_check × 3 rules (AML-001, KYC-002, SOX-003)
  generate_decision
  record_human_review

Usage:
  uv run python scripts/seed_demo_application.py
  uv run python scripts/seed_demo_application.py --application-id demo-001
  uv run python scripts/seed_demo_application.py --application-id demo-001 --verbose
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
from src.models.events import (
    ComplianceClearanceIssued,
    ComplianceCheckRequested,
    CreditAnalysisRequested,
)
from src.mcp.tools import _dispatch_tool

# Wire upcasters
import src.upcasting.upcasters  # noqa: F401
from src.event_store import set_registry
from src.upcasting.registry import registry

set_registry(registry)


def _step(label: str, start: float, result: dict, *, verbose: bool = False) -> None:
    elapsed_ms = (time.monotonic() - start) * 1000
    status = "✓" if "error_type" not in result else "✗"
    print(f"  {status} [{elapsed_ms:6.1f}ms] {label}")
    if verbose or "error_type" in result:
        print(f"    → {json.dumps(result, indent=2)}")


async def seed_application(
    pool: asyncpg.Pool,
    application_id: str,
    verbose: bool = False,
) -> None:
    """Run the full application lifecycle via MCP tool calls."""
    store = EventStore(pool=pool)

    credit_agent_id = f"agent-credit-{uuid4().hex[:8]}"
    fraud_agent_id = f"agent-fraud-{uuid4().hex[:8]}"
    orchestrator_agent_id = f"agent-orch-{uuid4().hex[:8]}"

    credit_session_id = f"session-credit-{uuid4().hex[:8]}"
    fraud_session_id = f"session-fraud-{uuid4().hex[:8]}"
    orch_session_id = f"session-orch-{uuid4().hex[:8]}"

    now = datetime.now(tz=timezone.utc)
    total_start = time.monotonic()

    print(f"\n{'='*60}")
    print(f"Seeding application: {application_id}")
    print(f"Started at: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print(f"{'='*60}\n")

    # ── 1. Start agent sessions ───────────────────────────────────────────────
    print("Phase A: Agent Session Initialisation (Gas Town Pattern)")

    t = time.monotonic()
    result = await _dispatch_tool("start_agent_session", {
        "agent_id": credit_agent_id,
        "session_id": credit_session_id,
        "context_source": "cold_start",
        "model_version": "credit-v2.3",
        "context_token_count": 4200,
    }, store)
    _step("start_agent_session (credit)", t, json.loads(result[0].text), verbose=verbose)

    t = time.monotonic()
    result = await _dispatch_tool("start_agent_session", {
        "agent_id": fraud_agent_id,
        "session_id": fraud_session_id,
        "context_source": "cold_start",
        "model_version": "fraud-v1.2",
        "context_token_count": 2100,
    }, store)
    _step("start_agent_session (fraud)", t, json.loads(result[0].text), verbose=verbose)

    t = time.monotonic()
    result = await _dispatch_tool("start_agent_session", {
        "agent_id": orchestrator_agent_id,
        "session_id": orch_session_id,
        "context_source": "cold_start",
        "model_version": "orch-v3.0",
        "context_token_count": 8000,
    }, store)
    _step("start_agent_session (orchestrator)", t, json.loads(result[0].text), verbose=verbose)

    # ── 2. Submit application ─────────────────────────────────────────────────
    print("\nPhase B: Application Submission")

    t = time.monotonic()
    result = await _dispatch_tool("submit_application", {
        "application_id": application_id,
        "applicant_id": f"applicant-{application_id}",
        "requested_amount_usd": 750_000.0,
        "loan_purpose": "Commercial real estate acquisition",
        "submission_channel": "api",
    }, store)
    data = json.loads(result[0].text)
    _step("submit_application", t, data, verbose=verbose)

    if "error_type" in data:
        print(f"\n⚠ Application submission failed — stream may already exist. Aborting.")
        return

    # ── 3. Advance to AWAITING_ANALYSIS ──────────────────────────────────────
    # (CreditAnalysisRequested is a direct append — no MCP tool for this event)
    loan_version = await store.stream_version(f"loan-{application_id}")
    await store.append(
        f"loan-{application_id}",
        [CreditAnalysisRequested(
            application_id=application_id,
            assigned_agent_id=credit_agent_id,
            requested_at=datetime.now(tz=timezone.utc),
            priority="HIGH",
        )],
        expected_version=loan_version,
    )

    # ── 4. Record credit analysis ─────────────────────────────────────────────
    print("\nPhase C: AI Analysis")

    t = time.monotonic()
    result = await _dispatch_tool("record_credit_analysis", {
        "application_id": application_id,
        "agent_id": credit_agent_id,
        "session_id": credit_session_id,
        "model_version": "credit-v2.3",
        "confidence_score": 0.84,
        "risk_tier": "MEDIUM",
        "recommended_limit_usd": 650_000.0,
        "duration_ms": 1450,
        "input_data": {
            "applicant_id": f"applicant-{application_id}",
            "bureau_score": 742,
            "dti_ratio": 0.32,
            "ltv_ratio": 0.68,
        },
    }, store)
    _step("record_credit_analysis", t, json.loads(result[0].text), verbose=verbose)

    # ── 5. Record fraud screening ─────────────────────────────────────────────
    t = time.monotonic()
    result = await _dispatch_tool("record_fraud_screening", {
        "application_id": application_id,
        "agent_id": fraud_agent_id,
        "session_id": fraud_session_id,
        "fraud_score": 0.08,
        "anomaly_flags": [],
        "screening_model_version": "fraud-v1.2",
        "input_data": {
            "applicant_id": f"applicant-{application_id}",
            "ip_reputation": "clean",
            "device_fingerprint": "known",
        },
    }, store)
    _step("record_fraud_screening", t, json.loads(result[0].text), verbose=verbose)

    # ── 6. Request compliance checks ─────────────────────────────────────────
    print("\nPhase D: Compliance")

    loan_version = await store.stream_version(f"loan-{application_id}")
    await store.append(
        f"loan-{application_id}",
        [ComplianceCheckRequested(
            application_id=application_id,
            regulation_set_version="REG-2026-Q1",
            checks_required=["AML-001", "KYC-002", "SOX-003"],
        )],
        expected_version=loan_version,
    )
    print(f"  ✓ [  direct] compliance_check_requested (AML-001, KYC-002, SOX-003)")

    # ── 7. Record compliance checks ───────────────────────────────────────────
    for rule_id, rule_version in [
        ("AML-001", "v4.1"),
        ("KYC-002", "v2.3"),
        ("SOX-003", "v1.0"),
    ]:
        t = time.monotonic()
        result = await _dispatch_tool("record_compliance_check", {
            "application_id": application_id,
            "rule_id": rule_id,
            "rule_version": rule_version,
            "passed": True,
            "evidence_hash": f"evidence-{rule_id.lower()}-{application_id[:8]}",
        }, store)
        _step(f"record_compliance_check ({rule_id})", t, json.loads(result[0].text), verbose=verbose)

    # ── 8. Issue compliance clearance ─────────────────────────────────────────
    loan_version = await store.stream_version(f"loan-{application_id}")
    await store.append(
        f"loan-{application_id}",
        [ComplianceClearanceIssued(
            application_id=application_id,
            regulation_set_version="REG-2026-Q1",
            issued_at=datetime.now(tz=timezone.utc),
            checks_passed=["AML-001", "KYC-002", "SOX-003"],
        )],
        expected_version=loan_version,
    )
    print(f"  ✓ [  direct] compliance_clearance_issued (all checks passed)")

    # ── 9. Generate decision ──────────────────────────────────────────────────
    print("\nPhase E: Decision")

    credit_session_ref = f"agent-{credit_agent_id}-{credit_session_id}"
    fraud_session_ref = f"agent-{fraud_agent_id}-{fraud_session_id}"

    t = time.monotonic()
    result = await _dispatch_tool("generate_decision", {
        "application_id": application_id,
        "orchestrator_agent_id": orchestrator_agent_id,
        "recommendation": "APPROVE",
        "confidence_score": 0.84,
        "contributing_agent_sessions": [credit_session_ref, fraud_session_ref],
        "decision_basis_summary": (
            "Credit analysis: MEDIUM risk, score 0.84, recommended limit $650K. "
            "Fraud screening: low risk score 0.08, no anomalies. "
            "All compliance checks (AML-001, KYC-002, SOX-003) passed under REG-2026-Q1."
        ),
        "model_versions": {
            credit_agent_id: "credit-v2.3",
            fraud_agent_id: "fraud-v1.2",
        },
    }, store)
    _step("generate_decision", t, json.loads(result[0].text), verbose=verbose)

    # ── 10. Human review ──────────────────────────────────────────────────────
    print("\nPhase F: Human Review")

    t = time.monotonic()
    result = await _dispatch_tool("record_human_review", {
        "application_id": application_id,
        "reviewer_id": "reviewer-sarah-chen",
        "override": False,
        "final_decision": "APPROVED",
    }, store)
    data = json.loads(result[0].text)
    _step("record_human_review", t, data, verbose=verbose)

    # ── Summary ───────────────────────────────────────────────────────────────
    total_ms = (time.monotonic() - total_start) * 1000
    final_version = await store.stream_version(f"loan-{application_id}")

    print(f"\n{'='*60}")
    print(f"✅ Lifecycle complete in {total_ms:.1f}ms")
    print(f"   application_id : {application_id}")
    print(f"   final_decision : {data.get('final_decision', 'unknown')}")
    print(f"   loan_stream_v  : {final_version}")
    print(f"{'='*60}\n")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed a demo application through the full Apex Financial lifecycle."
    )
    parser.add_argument(
        "--application-id",
        default=None,
        help="Application ID to use (default: auto-generated)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full response payloads for each step",
    )
    args = parser.parse_args()

    application_id = args.application_id or f"demo-{uuid4().hex[:12]}"

    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql://ledger:ledger_dev@localhost:5432/ledger",
    )
    pool = await create_pool(dsn)
    await run_migrations(pool)

    try:
        await seed_application(pool, application_id, verbose=args.verbose)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
