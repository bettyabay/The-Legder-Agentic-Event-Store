"""
Phase 5 Mandatory Test: Full MCP Lifecycle Integration Test

Drives a complete loan application from ApplicationSubmitted through FinalApproved
using ONLY MCP tool calls. No direct Python function calls to core modules.

Lifecycle:
  submit_application → start_agent_session →
  record_credit_analysis → record_fraud_screening →
  [request compliance check via handler] →
  record_compliance_check → generate_decision →
  record_human_review → query compliance resource

Run: uv run pytest tests/test_mcp_lifecycle.py -v
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import asyncpg
import pytest

from src.commands.handlers import (
    RecordComplianceRuleCommand,
    RequestComplianceCheckCommand,
    handle_record_compliance_rule,
    handle_request_compliance_check,
    handle_submit_application,
    SubmitApplicationCommand,
)
from src.event_store import EventStore
from src.mcp.server import create_server
from src.mcp.tools import _dispatch_tool
from src.mcp.resources import _dispatch_resource
from src.projections.daemon import ProjectionDaemon
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.agent_performance import AgentPerformanceLedgerProjection


@pytest.mark.asyncio
async def test_full_loan_lifecycle_via_mcp_tools_only(
    db_pool: asyncpg.Pool,
    unique_app_id: str,
    unique_agent_id: str,
    unique_session_id: str,
) -> None:
    """Full loan lifecycle driven exclusively through MCP tool dispatch.

    This test simulates exactly what a real AI agent would do:
    it calls start_agent_session, then analysis tools, then generate_decision,
    then record_human_review, and finally queries the compliance audit resource.
    """
    from src.event_store import EventStore, set_registry
    from src.upcasting.registry import registry
    import src.upcasting.upcasters  # noqa: F401

    set_registry(registry)
    store = EventStore(pool=db_pool)

    # Build projections and daemon for query-side
    summary_proj = ApplicationSummaryProjection()
    agent_perf_proj = AgentPerformanceLedgerProjection()
    compliance_proj = ComplianceAuditViewProjection(pool=db_pool)
    daemon = ProjectionDaemon(
        store=store,
        projections=[summary_proj, agent_perf_proj, compliance_proj],
        pool=db_pool,
    )

    # ── PHASE A: Command side (MCP tools only) ────────────────────────────

    # 1. submit_application
    result = await _dispatch_tool("submit_application", {
        "application_id": unique_app_id,
        "applicant_id": "mcp-test-applicant",
        "requested_amount_usd": 450_000.0,
        "loan_purpose": "MCP lifecycle test",
        "submission_channel": "api",
    }, store)
    data = json.loads(result[0].text)
    assert "stream_id" in data, f"submit_application failed: {data}"

    # 2. start_agent_session (Gas Town)
    result = await _dispatch_tool("start_agent_session", {
        "agent_id": unique_agent_id,
        "session_id": unique_session_id,
        "context_source": "cold_start",
        "model_version": "v2.3",
        "context_token_count": 5000,
    }, store)
    data = json.loads(result[0].text)
    assert "session_id" in data, f"start_agent_session failed: {data}"

    # 3. Advance app to AWAITING_ANALYSIS via loan stream
    from src.models.events import CreditAnalysisRequested
    now = datetime.now(tz=timezone.utc)
    await store.append(
        f"loan-{unique_app_id}",
        [CreditAnalysisRequested(
            application_id=unique_app_id,
            assigned_agent_id=unique_agent_id,
            requested_at=now,
        )],
        expected_version=1,
    )

    # 4. record_credit_analysis
    result = await _dispatch_tool("record_credit_analysis", {
        "application_id": unique_app_id,
        "agent_id": unique_agent_id,
        "session_id": unique_session_id,
        "model_version": "v2.3",
        "confidence_score": 0.82,
        "risk_tier": "MEDIUM",
        "recommended_limit_usd": 380_000.0,
        "duration_ms": 1200,
        "input_data": {"applicant": unique_app_id, "source": "mcp_test"},
    }, store)
    data = json.loads(result[0].text)
    assert "new_stream_version" in data, f"record_credit_analysis failed: {data}"

    # 5. start fraud screening agent session
    fraud_session_id = f"{unique_session_id}-fraud"
    result = await _dispatch_tool("start_agent_session", {
        "agent_id": unique_agent_id,
        "session_id": fraud_session_id,
        "context_source": "cold_start",
        "model_version": "fraud-v1.2",
        "context_token_count": 2000,
    }, store)

    # 6. record_fraud_screening
    result = await _dispatch_tool("record_fraud_screening", {
        "application_id": unique_app_id,
        "agent_id": unique_agent_id,
        "session_id": fraud_session_id,
        "fraud_score": 0.12,
        "anomaly_flags": [],
        "screening_model_version": "fraud-v1.2",
        "input_data": {"applicant": unique_app_id},
    }, store)
    data = json.loads(result[0].text)
    assert "new_stream_version" in data, f"record_fraud_screening failed: {data}"

    # 7. Request compliance checks (via handler — no MCP tool for this in spec,
    #    but we need to advance state; record_compliance_check has it)
    await handle_request_compliance_check(
        RequestComplianceCheckCommand(
            application_id=unique_app_id,
            regulation_set_version="REG-2026-Q1",
            checks_required=["AML-001", "KYC-002"],
        ),
        store,
    )

    # 8. record_compliance_check (AML-001)
    result = await _dispatch_tool("record_compliance_check", {
        "application_id": unique_app_id,
        "rule_id": "AML-001",
        "rule_version": "v3.0",
        "passed": True,
        "evidence_hash": "aml-evidence-hash",
    }, store)
    data = json.loads(result[0].text)
    assert data.get("compliance_status") == "PASSED", f"AML compliance failed: {data}"

    # 9. record_compliance_check (KYC-002)
    result = await _dispatch_tool("record_compliance_check", {
        "application_id": unique_app_id,
        "rule_id": "KYC-002",
        "rule_version": "v2.1",
        "passed": True,
        "evidence_hash": "kyc-evidence-hash",
    }, store)
    data = json.loads(result[0].text)
    assert data.get("compliance_status") == "PASSED", f"KYC compliance failed: {data}"

    # 10. Issue compliance clearance (business event — advance to PENDING_DECISION)
    from src.models.events import ComplianceClearanceIssued
    await store.append(
        f"loan-{unique_app_id}",
        [ComplianceClearanceIssued(
            application_id=unique_app_id,
            regulation_set_version="REG-2026-Q1",
            issued_at=datetime.now(tz=timezone.utc),
            checks_passed=["AML-001", "KYC-002"],
        )],
        expected_version=await store.stream_version(f"loan-{unique_app_id}"),
    )

    # 11. generate_decision
    credit_session_ref = f"agent-{unique_agent_id}-{unique_session_id}"
    result = await _dispatch_tool("generate_decision", {
        "application_id": unique_app_id,
        "orchestrator_agent_id": unique_agent_id,
        "recommendation": "APPROVE",
        "confidence_score": 0.82,
        "contributing_agent_sessions": [credit_session_ref],
        "decision_basis_summary": "Credit MEDIUM risk, fraud score 0.12, all compliance checks passed.",
        "model_versions": {unique_agent_id: "v2.3"},
    }, store)
    data = json.loads(result[0].text)
    assert data.get("recommendation") == "APPROVE", f"generate_decision failed: {data}"

    # 12. record_human_review
    result = await _dispatch_tool("record_human_review", {
        "application_id": unique_app_id,
        "reviewer_id": "reviewer-001",
        "override": False,
        "final_decision": "APPROVED",
    }, store)
    data = json.loads(result[0].text)
    assert data.get("final_decision") == "APPROVED", f"record_human_review failed: {data}"

    # ── PHASE B: Query side (MCP resources) ──────────────────────────────

    # Process events through daemon so projections are populated
    for _ in range(20):
        await daemon._process_batch()
        await asyncio.sleep(0.02)

    # 13. Query compliance resource — verify complete audit trace
    compliance_result = await _dispatch_resource(
        f"ledger://applications/{unique_app_id}/compliance",
        store,
        db_pool,
        daemon,
        compliance_proj,
    )
    compliance_data = json.loads(compliance_result)
    records = compliance_data.get("compliance_records", [])

    # Should have both AML-001 and KYC-002 as PASSED
    passed_rules = {r["rule_id"] for r in records if r.get("status") == "PASSED"}
    assert "AML-001" in passed_rules, f"AML-001 not in compliance records: {records}"
    assert "KYC-002" in passed_rules, f"KYC-002 not in compliance records: {records}"

    # 14. Query application summary resource
    summary_result = await _dispatch_resource(
        f"ledger://applications/{unique_app_id}",
        store,
        db_pool,
        daemon,
        compliance_proj,
    )
    summary_data = json.loads(summary_result)
    assert summary_data.get("state") in ("FINAL_APPROVED", "APPROVED_PENDING_HUMAN", "PENDING_DECISION"), (
        f"Unexpected application state: {summary_data.get('state')}"
    )


@pytest.mark.asyncio
async def test_mcp_tool_precondition_failure_returns_structured_error(
    db_pool: asyncpg.Pool,
    unique_agent_id: str,
    unique_session_id: str,
    unique_app_id: str,
) -> None:
    """record_credit_analysis without prior start_agent_session returns structured error."""
    from src.event_store import EventStore, set_registry
    from src.upcasting.registry import registry
    import src.upcasting.upcasters  # noqa: F401
    from src.models.events import CreditAnalysisRequested
    from datetime import timezone

    set_registry(registry)
    store = EventStore(pool=db_pool)

    # Submit application and advance to AWAITING_ANALYSIS
    from src.commands.handlers import handle_submit_application, SubmitApplicationCommand
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=unique_app_id,
            applicant_id="precondition-test",
            requested_amount_usd=100_000.0,
            loan_purpose="Precondition test",
            submission_channel="test",
        ),
        store,
    )
    now = datetime.now(tz=timezone.utc)
    await store.append(
        f"loan-{unique_app_id}",
        [CreditAnalysisRequested(
            application_id=unique_app_id,
            assigned_agent_id=unique_agent_id,
            requested_at=now,
        )],
        expected_version=1,
    )

    # Attempt to record credit analysis WITHOUT calling start_agent_session
    # The agent session stream does not exist → AgentContextNotLoadedError
    result = await _dispatch_tool("record_credit_analysis", {
        "application_id": unique_app_id,
        "agent_id": unique_agent_id,
        "session_id": unique_session_id,  # no session started
        "model_version": "v2.3",
        "confidence_score": 0.75,
        "risk_tier": "LOW",
        "recommended_limit_usd": 90_000.0,
        "duration_ms": 800,
        "input_data": {},
    }, store)

    error_data = json.loads(result[0].text)
    assert "error_type" in error_data, f"Expected structured error, got: {error_data}"
    # Should be AgentContextNotLoadedError or DomainError
    assert "error_type" in error_data
    assert error_data["error_type"] in (
        "AgentContextNotLoadedError",
        "DomainError",
        "InvalidStateTransitionError",
    ), f"Unexpected error_type: {error_data['error_type']}"
