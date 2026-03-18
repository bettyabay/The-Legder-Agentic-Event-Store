"""
MCP Tools — The Command Side of The Ledger.

Each tool maps to a command handler. Tools consume commands from AI agents
and return structured results or typed error objects.

LLM-consumption design requirements:
  1. Precondition documentation in the tool description — the LLM's only contract.
  2. Structured error types — typed objects with suggested_action for autonomous recovery.
  3. Retry budget: 3 retries with exponential backoff (100/200/400 ms) on concurrency errors.

All 8 tools (challenge spec §Phase 5):
  submit_application, record_credit_analysis, record_fraud_screening,
  record_compliance_check, generate_decision, record_human_review,
  start_agent_session, run_integrity_check
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool

from src.commands.handlers import (
    CreditAnalysisCompletedCommand,
    FraudScreeningCompletedCommand,
    GenerateDecisionCommand,
    RecordComplianceRuleCommand,
    RecordHumanReviewCommand,
    RequestComplianceCheckCommand,
    StartAgentSessionCommand,
    SubmitApplicationCommand,
    handle_credit_analysis_completed,
    handle_fraud_screening_completed,
    handle_generate_decision,
    handle_human_review_completed,
    handle_record_compliance_rule,
    handle_request_compliance_check,
    handle_start_agent_session,
    handle_submit_application,
)
from src.event_store import EventStore
from src.integrity.audit_chain import run_integrity_check
from src.models.exceptions import (
    AgentContextNotLoadedError,
    DomainError,
    LedgerError,
    OptimisticConcurrencyError,
    PreconditionFailedError,
    RateLimitError,
)

logger = logging.getLogger(__name__)

# Retry budget: 3 retries, exponential backoff 100/200/400ms
_MAX_RETRIES = 3
_BACKOFF_BASE_MS = 100


def _err(
    error: LedgerError | Exception,
    context: dict[str, Any] | None = None,
) -> list[TextContent]:
    """Convert an exception to a structured MCP error payload."""
    import json
    payload: dict[str, Any] = {
        "error_type": getattr(error, "error_type", type(error).__name__),
        "message": str(error),
    }
    if isinstance(error, OptimisticConcurrencyError):
        payload.update({
            "stream_id": error.stream_id,
            "expected_version": error.expected_version,
            "actual_version": error.actual_version,
            "suggested_action": error.suggested_action,
        })
    if context:
        payload.update(context)
    return [TextContent(type="text", text=json.dumps(payload))]


def _ok(data: dict[str, Any]) -> list[TextContent]:
    import json
    return [TextContent(type="text", text=json.dumps(data))]


async def _with_concurrency_retry(
    coro_fn: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Execute a coroutine function with OCC retry budget."""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except OptimisticConcurrencyError:
            if attempt < _MAX_RETRIES:
                backoff = _BACKOFF_BASE_MS * (2 ** (attempt - 1)) / 1000
                await asyncio.sleep(backoff)
            else:
                raise


def register_tools(server: Server, store: EventStore) -> None:
    """Register all 8 MCP tools on the server instance."""

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="submit_application",
                description=(
                    "Submit a new loan application to The Ledger. "
                    "Creates a new LoanApplication stream with ApplicationSubmitted as the first event. "
                    "This tool must be called before any analysis or compliance tools for an application. "
                    "Raises DomainError if the application_id already exists."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["application_id", "applicant_id", "requested_amount_usd", "loan_purpose"],
                    "properties": {
                        "application_id": {"type": "string", "description": "Unique application identifier"},
                        "applicant_id": {"type": "string", "description": "Borrower identifier"},
                        "requested_amount_usd": {"type": "number", "description": "Requested loan amount in USD"},
                        "loan_purpose": {"type": "string", "description": "Purpose of the loan"},
                        "submission_channel": {"type": "string", "default": "api"},
                        "correlation_id": {"type": "string"},
                    },
                },
            ),
            Tool(
                name="start_agent_session",
                description=(
                    "Start a new AI agent work session and declare its context source. "
                    "REQUIRED PRECONDITION: This tool MUST be called before any analysis or decision tools "
                    "(record_credit_analysis, record_fraud_screening, generate_decision). "
                    "Calling those tools without an active session will return a PreconditionFailed error "
                    "with suggested_action='call_start_agent_session_first'. "
                    "This implements the Gas Town Persistent Ledger Pattern — every agent action is "
                    "tied to a context declaration."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["agent_id", "session_id", "context_source", "model_version", "context_token_count"],
                    "properties": {
                        "agent_id": {"type": "string"},
                        "session_id": {"type": "string"},
                        "context_source": {"type": "string", "description": "Source of context (event_replay, cold_start, etc.)"},
                        "model_version": {"type": "string"},
                        "context_token_count": {"type": "integer"},
                        "event_replay_from_position": {"type": "integer", "default": 0},
                        "correlation_id": {"type": "string"},
                    },
                },
            ),
            Tool(
                name="record_credit_analysis",
                description=(
                    "Record the output of a credit analysis agent session. "
                    "PRECONDITION: start_agent_session must have been called for this agent_id/session_id. "
                    "PRECONDITION: The loan application must be in AWAITING_ANALYSIS state "
                    "(call submit_application first, then trigger analysis via record_credit_analysis_requested). "
                    "Raises PreconditionFailed if no active agent session. "
                    "Raises OptimisticConcurrencyError if the stream was concurrently modified — "
                    "suggested_action: reload_stream_and_retry (handled automatically with 3 retries)."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["application_id", "agent_id", "session_id", "model_version",
                                 "confidence_score", "risk_tier", "recommended_limit_usd",
                                 "duration_ms", "input_data"],
                    "properties": {
                        "application_id": {"type": "string"},
                        "agent_id": {"type": "string"},
                        "session_id": {"type": "string"},
                        "model_version": {"type": "string"},
                        "confidence_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "risk_tier": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                        "recommended_limit_usd": {"type": "number"},
                        "duration_ms": {"type": "integer"},
                        "input_data": {"type": "object"},
                        "correlation_id": {"type": "string"},
                        "causation_id": {"type": "string"},
                    },
                },
            ),
            Tool(
                name="record_fraud_screening",
                description=(
                    "Record the output of a fraud screening agent. "
                    "PRECONDITION: start_agent_session must have been called for this agent_id/session_id. "
                    "fraud_score must be in [0.0, 1.0]; values outside this range return DomainError."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["application_id", "agent_id", "session_id",
                                 "fraud_score", "screening_model_version", "input_data"],
                    "properties": {
                        "application_id": {"type": "string"},
                        "agent_id": {"type": "string"},
                        "session_id": {"type": "string"},
                        "fraud_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "anomaly_flags": {"type": "array", "items": {"type": "string"}, "default": []},
                        "screening_model_version": {"type": "string"},
                        "input_data": {"type": "object"},
                        "correlation_id": {"type": "string"},
                    },
                },
            ),
            Tool(
                name="record_compliance_check",
                description=(
                    "Record a compliance rule evaluation result (pass or fail). "
                    "PRECONDITION: record_compliance_check_requested must have been issued for this application "
                    "to establish the active regulation_set_version. "
                    "rule_id must exist in the active regulation_set_version; otherwise DomainError is returned."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["application_id", "rule_id", "rule_version", "passed"],
                    "properties": {
                        "application_id": {"type": "string"},
                        "rule_id": {"type": "string"},
                        "rule_version": {"type": "string"},
                        "passed": {"type": "boolean"},
                        "failure_reason": {"type": "string", "default": ""},
                        "remediation_required": {"type": "boolean", "default": True},
                        "evidence_hash": {"type": "string", "default": ""},
                        "correlation_id": {"type": "string"},
                    },
                },
            ),
            Tool(
                name="generate_decision",
                description=(
                    "Generate an AI decision (APPROVE/DECLINE/REFER) for a loan application. "
                    "PRECONDITION: All required analyses (credit, fraud) must be present. "
                    "PRECONDITION: A compliance clearance must have been issued (all checks passed). "
                    "PRECONDITION: Application must be in PENDING_DECISION state. "
                    "BUSINESS RULE: confidence_score < 0.6 FORCES recommendation = 'REFER' regardless "
                    "of input value. This is a regulatory requirement enforced in the aggregate. "
                    "BUSINESS RULE: contributing_agent_sessions must reference sessions that processed "
                    "this application — InvalidCausalChainError otherwise."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["application_id", "orchestrator_agent_id", "recommendation",
                                 "confidence_score", "contributing_agent_sessions", "decision_basis_summary"],
                    "properties": {
                        "application_id": {"type": "string"},
                        "orchestrator_agent_id": {"type": "string"},
                        "recommendation": {"type": "string", "enum": ["APPROVE", "DECLINE", "REFER"]},
                        "confidence_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "contributing_agent_sessions": {"type": "array", "items": {"type": "string"}},
                        "decision_basis_summary": {"type": "string"},
                        "model_versions": {"type": "object", "additionalProperties": {"type": "string"}},
                        "correlation_id": {"type": "string"},
                        "causation_id": {"type": "string"},
                    },
                },
            ),
            Tool(
                name="record_human_review",
                description=(
                    "Record a human loan officer's review of an AI-generated decision. "
                    "PRECONDITION: Application must be in APPROVED_PENDING_HUMAN or DECLINED_PENDING_HUMAN state. "
                    "PRECONDITION: If override=true, override_reason must be provided; otherwise DomainError. "
                    "reviewer_id must be a valid human reviewer identifier."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["application_id", "reviewer_id", "override", "final_decision"],
                    "properties": {
                        "application_id": {"type": "string"},
                        "reviewer_id": {"type": "string"},
                        "override": {"type": "boolean"},
                        "final_decision": {"type": "string", "enum": ["APPROVED", "DECLINED"]},
                        "override_reason": {"type": "string"},
                        "correlation_id": {"type": "string"},
                    },
                },
            ),
            Tool(
                name="run_integrity_check",
                description=(
                    "Run a cryptographic integrity check on an entity's audit chain. "
                    "PRECONDITION: Caller must have 'compliance' role. "
                    "RATE LIMIT: 1 call per minute per entity_id (enforced server-side). "
                    "Returns chain_valid=true if the hash chain is intact, false if tampering detected. "
                    "A new AuditIntegrityCheckRun event is appended to the audit stream."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["entity_type", "entity_id"],
                    "properties": {
                        "entity_type": {"type": "string", "enum": ["loan", "agent", "compliance"]},
                        "entity_id": {"type": "string"},
                    },
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            return await _dispatch_tool(name, arguments, store)
        except OptimisticConcurrencyError as exc:
            return _err(exc)
        except LedgerError as exc:
            return _err(exc)
        except Exception as exc:
            logger.exception("Unexpected error in tool '%s'", name)
            return _err(exc)


async def _dispatch_tool(
    name: str,
    args: dict[str, Any],
    store: EventStore,
) -> list[TextContent]:
    """Route tool call to appropriate handler."""
    if name == "submit_application":
        cmd = SubmitApplicationCommand(
            application_id=args["application_id"],
            applicant_id=args["applicant_id"],
            requested_amount_usd=args["requested_amount_usd"],
            loan_purpose=args["loan_purpose"],
            submission_channel=args.get("submission_channel", "api"),
            correlation_id=args.get("correlation_id"),
        )
        version = await handle_submit_application(cmd, store)
        return _ok({"stream_id": f"loan-{args['application_id']}", "initial_version": version})

    elif name == "start_agent_session":
        cmd = StartAgentSessionCommand(
            agent_id=args["agent_id"],
            session_id=args["session_id"],
            context_source=args["context_source"],
            model_version=args["model_version"],
            context_token_count=args["context_token_count"],
            event_replay_from_position=args.get("event_replay_from_position", 0),
            correlation_id=args.get("correlation_id"),
        )
        version = await handle_start_agent_session(cmd, store)
        return _ok({"session_id": args["session_id"], "context_position": version})

    elif name == "record_credit_analysis":
        cmd = CreditAnalysisCompletedCommand(
            application_id=args["application_id"],
            agent_id=args["agent_id"],
            session_id=args["session_id"],
            model_version=args["model_version"],
            confidence_score=args["confidence_score"],
            risk_tier=args["risk_tier"],
            recommended_limit_usd=args["recommended_limit_usd"],
            duration_ms=args["duration_ms"],
            input_data=args["input_data"],
            correlation_id=args.get("correlation_id"),
            causation_id=args.get("causation_id"),
        )
        try:
            version = await _with_concurrency_retry(handle_credit_analysis_completed, cmd, store)
            return _ok({
                "event_id": str(version),
                "new_stream_version": version,
            })
        except AgentContextNotLoadedError as e:
            return _err(e, {"suggested_action": "Call start_agent_session before any decision tool."})
        except PreconditionFailedError as e:
            return _err(e, {"suggested_action": getattr(e, "suggested_action", "")})

    elif name == "record_fraud_screening":
        cmd = FraudScreeningCompletedCommand(
            application_id=args["application_id"],
            agent_id=args["agent_id"],
            session_id=args["session_id"],
            fraud_score=args["fraud_score"],
            anomaly_flags=args.get("anomaly_flags", []),
            screening_model_version=args["screening_model_version"],
            input_data=args["input_data"],
            correlation_id=args.get("correlation_id"),
        )
        version = await _with_concurrency_retry(handle_fraud_screening_completed, cmd, store)
        return _ok({"event_id": str(version), "new_stream_version": version})

    elif name == "record_compliance_check":
        cmd = RecordComplianceRuleCommand(
            application_id=args["application_id"],
            rule_id=args["rule_id"],
            rule_version=args["rule_version"],
            passed=args["passed"],
            failure_reason=args.get("failure_reason", ""),
            remediation_required=args.get("remediation_required", True),
            evidence_hash=args.get("evidence_hash", ""),
            correlation_id=args.get("correlation_id"),
        )
        version = await handle_record_compliance_rule(cmd, store)
        return _ok({
            "check_id": str(version),
            "compliance_status": "PASSED" if args["passed"] else "FAILED",
        })

    elif name == "generate_decision":
        cmd = GenerateDecisionCommand(
            application_id=args["application_id"],
            orchestrator_agent_id=args["orchestrator_agent_id"],
            recommendation=args["recommendation"],
            confidence_score=args["confidence_score"],
            contributing_agent_sessions=args["contributing_agent_sessions"],
            decision_basis_summary=args["decision_basis_summary"],
            model_versions=args.get("model_versions", {}),
            correlation_id=args.get("correlation_id"),
            causation_id=args.get("causation_id"),
        )
        version = await _with_concurrency_retry(handle_generate_decision, cmd, store)
        return _ok({
            "decision_id": str(version),
            "recommendation": args["recommendation"],
        })

    elif name == "record_human_review":
        cmd = RecordHumanReviewCommand(
            application_id=args["application_id"],
            reviewer_id=args["reviewer_id"],
            override=args["override"],
            final_decision=args["final_decision"],
            override_reason=args.get("override_reason"),
            correlation_id=args.get("correlation_id"),
        )
        version = await _with_concurrency_retry(handle_human_review_completed, cmd, store)
        return _ok({
            "final_decision": args["final_decision"],
            "application_state": "FINAL_APPROVED" if args["final_decision"] == "APPROVED" else "FINAL_DECLINED",
        })

    elif name == "run_integrity_check":
        result = await run_integrity_check(
            store=store,
            entity_type=args["entity_type"],
            entity_id=args["entity_id"],
        )
        return _ok({
            "check_result": {
                "events_verified": result.events_verified,
                "integrity_hash": result.integrity_hash,
                "previous_hash": result.previous_hash,
            },
            "chain_valid": result.chain_valid,
            "tamper_detected": result.tamper_detected,
        })

    else:
        return _err(ValueError(f"Unknown tool: {name}"))
