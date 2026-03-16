"""
Query History — Event Timeline and Compliance Audit for an Application.

Calls MCP resources directly to retrieve and display:
  - Full audit trail (event-by-event timeline)
  - Compliance audit view (with optional temporal query)
  - Projection lag metadata

Usage:
  uv run python scripts/query_history.py --application-id demo-001
  uv run python scripts/query_history.py --application-id demo-001 --compliance-only
  uv run python scripts/query_history.py --application-id demo-001 --as-of 2026-03-16T12:00:00Z
  uv run python scripts/query_history.py --application-id demo-001 --verbose
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone

import asyncpg

from src.db.pool import create_pool, run_migrations
from src.event_store import EventStore
from src.mcp.resources import _dispatch_resource
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon
from src.projections.application_summary import ApplicationSummaryProjection

# Wire upcasters
import src.upcasting.upcasters  # noqa: F401
from src.event_store import set_registry
from src.upcasting.registry import registry

set_registry(registry)

# ─── Formatting helpers ───────────────────────────────────────────────────────

_EVENT_SYMBOLS: dict[str, str] = {
    "ApplicationSubmitted": "📋",
    "CreditAnalysisRequested": "🔍",
    "CreditAnalysisCompleted": "📊",
    "FraudScreeningCompleted": "🛡 ",
    "ComplianceCheckRequested": "📜",
    "ComplianceRulePassed": "✅",
    "ComplianceRuleFailed": "❌",
    "ComplianceClearanceIssued": "🔓",
    "DecisionGenerated": "⚖ ",
    "HumanReviewCompleted": "👤",
    "ApplicationApproved": "🎉",
    "ApplicationDeclined": "🚫",
    "AgentContextLoaded": "🤖",
    "AuditIntegrityCheckRun": "🔐",
}


def _fmt_event(event: dict) -> str:
    symbol = _EVENT_SYMBOLS.get(event["event_type"], "  ")
    ts = event.get("recorded_at", "")[:19].replace("T", " ")
    et = event["event_type"]
    pos = event.get("stream_position", "?")
    payload = event.get("payload", {})

    # Summarise key payload fields
    details: list[str] = []
    if "applicant_id" in payload:
        details.append(f"applicant={payload['applicant_id']}")
    if "requested_amount_usd" in payload:
        details.append(f"amount=${payload['requested_amount_usd']:,.0f}")
    if "risk_tier" in payload:
        details.append(f"risk={payload['risk_tier']}")
    if "confidence_score" in payload and payload["confidence_score"] is not None:
        details.append(f"confidence={payload['confidence_score']:.2f}")
    if "fraud_score" in payload:
        details.append(f"fraud_score={payload['fraud_score']:.2f}")
    if "rule_id" in payload:
        details.append(f"rule={payload['rule_id']}")
    if "recommendation" in payload:
        details.append(f"recommendation={payload['recommendation']}")
    if "final_decision" in payload:
        details.append(f"decision={payload['final_decision']}")

    detail_str = f"  ({', '.join(details)})" if details else ""
    return f"  [{pos:>3}] {symbol} {ts}  {et}{detail_str}"


def _fmt_compliance_record(record: dict) -> str:
    status = record.get("status", "?")
    icon = "✅" if status == "PASSED" else "❌" if status == "FAILED" else "⏳"
    rule_id = record.get("rule_id", "?")
    rule_version = record.get("rule_version", "?")
    eval_ts = (record.get("evaluation_timestamp") or "")[:19]
    return f"  {icon} {rule_id} (v{rule_version}) — {status}  [{eval_ts}]"


# ─── Main query logic ─────────────────────────────────────────────────────────


async def query_application(
    pool: asyncpg.Pool,
    application_id: str,
    compliance_only: bool = False,
    as_of: str | None = None,
    verbose: bool = False,
) -> None:
    store = EventStore(pool=pool)
    compliance_proj = ComplianceAuditViewProjection(pool=pool)
    summary_proj = ApplicationSummaryProjection()
    daemon = ProjectionDaemon(
        store=store,
        projections=[summary_proj, compliance_proj],
        pool=pool,
    )

    print(f"\n{'='*64}")
    print(f"Application Query: {application_id}")
    ts_label = f"as_of={as_of}" if as_of else "current"
    print(f"Timestamp: {ts_label}")
    print(f"{'='*64}")

    overall_start = time.monotonic()

    # ── A. Application Summary ────────────────────────────────────────────────
    if not compliance_only:
        t = time.monotonic()
        summary_uri = f"ledger://applications/{application_id}"
        summary_json = await _dispatch_resource(summary_uri, store, pool, daemon, compliance_proj)
        summary_ms = (time.monotonic() - t) * 1000
        summary = json.loads(summary_json)

        if "error" in summary:
            print(f"\n⚠ Application not found in projection (run seed or daemon first).")
            print(f"  Raw event stream will still be shown below.\n")
        else:
            print(f"\n📦 Application Summary  [{summary_ms:.1f}ms]")
            print(f"  State     : {summary.get('state', '?')}")
            print(f"  Applicant : {summary.get('applicant_id', '?')}")
            print(f"  Amount    : ${summary.get('requested_amount_usd') or 0:,.0f} requested")
            if summary.get("approved_amount_usd"):
                print(f"  Approved  : ${summary.get('approved_amount_usd'):,.0f}")
            if summary.get("risk_tier"):
                print(f"  Risk Tier : {summary.get('risk_tier')}")
            if summary.get("decision"):
                print(f"  Decision  : {summary.get('decision')}")
            print(f"  Last Event: {summary.get('last_event_type', '?')} @ "
                  f"{(summary.get('last_event_at') or '')[:19]}")

    # ── B. Audit Trail ────────────────────────────────────────────────────────
    if not compliance_only:
        print(f"\n📜 Audit Trail (all events)")
        t = time.monotonic()
        audit_uri = f"ledger://applications/{application_id}/audit-trail"
        if as_of:
            audit_uri += f"?to={as_of}"
        audit_json = await _dispatch_resource(audit_uri, store, pool, daemon, compliance_proj)
        audit_ms = (time.monotonic() - t) * 1000

        audit = json.loads(audit_json)
        events = audit.get("events", [])
        print(f"  {len(events)} events  [{audit_ms:.1f}ms]")

        if events:
            print()
            for ev in events:
                print(_fmt_event(ev))
                if verbose and ev.get("payload"):
                    print(f"       payload: {json.dumps(ev['payload'])}")

    # ── C. Compliance Audit View ──────────────────────────────────────────────
    print(f"\n🔒 Compliance Audit View")
    t = time.monotonic()
    compliance_uri = f"ledger://applications/{application_id}/compliance"
    if as_of:
        compliance_uri += f"?as_of={as_of}"
    compliance_json = await _dispatch_resource(
        compliance_uri, store, pool, daemon, compliance_proj
    )
    compliance_ms = (time.monotonic() - t) * 1000

    compliance_data = json.loads(compliance_json)
    records = compliance_data.get("compliance_records", [])
    print(f"  {len(records)} compliance records  [{compliance_ms:.1f}ms]")
    for rec in records:
        print(_fmt_compliance_record(rec))

    # ── D. Projection Health ──────────────────────────────────────────────────
    print(f"\n💓 Projection Lag")
    t = time.monotonic()
    health_json = await _dispatch_resource(
        "ledger://ledger/health", store, pool, daemon, compliance_proj
    )
    health_ms = (time.monotonic() - t) * 1000
    health = json.loads(health_json)
    lags = health.get("projection_lags_ms", {})
    for proj_name, lag_ms in lags.items():
        slo_indicator = "✓" if lag_ms < 2000 else "⚠"
        print(f"  {slo_indicator} {proj_name}: {lag_ms}ms")
    print(f"  (health query: {health_ms:.1f}ms)")

    total_ms = (time.monotonic() - overall_start) * 1000
    print(f"\n{'='*64}")
    print(f"Total query time: {total_ms:.1f}ms")
    print(f"{'='*64}\n")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Query complete history and compliance audit for an application."
    )
    parser.add_argument(
        "--application-id",
        required=True,
        help="Application ID to query",
    )
    parser.add_argument(
        "--compliance-only",
        action="store_true",
        help="Show only compliance records (skip audit trail)",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        metavar="ISO8601",
        help="Temporal query: show state as it existed at this timestamp (e.g. 2026-03-15T10:00:00Z)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full event payloads",
    )
    args = parser.parse_args()

    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql://ledger:ledger_dev@localhost:5432/ledger",
    )
    pool = await create_pool(dsn)
    await run_migrations(pool)

    try:
        await query_application(
            pool,
            application_id=args.application_id,
            compliance_only=args.compliance_only,
            as_of=args.as_of,
            verbose=args.verbose,
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
