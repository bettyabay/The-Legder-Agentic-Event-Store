# Contract: MCP Tools (Command Side)

**Status**: FROZEN after Phase 5 completion (Constitution Principle III)
**Source**: Challenge document Phase 5, "MCP Tools — The Command Side"
**File**: `src/mcp/tools.py`
**Transport**: MCP (stdio or SSE)

---

## Design Principles

1. **Zero business logic in tools**: All validation delegated to aggregates and
   command handlers. Tools are thin orchestrators.
2. **Structured error types**: Every error is a typed JSON object, not a string.
   LLM consumers need machine-readable errors to reason about recovery.
3. **Precondition documentation**: Each tool description explicitly states what
   must be true before calling. LLM consumers read this as the contract.
4. **Security model**: Caller-supplied identity (reviewer_id, agent_id). The MCP
   server records identity in event payloads for audit but does not validate
   credentials. Access control is a perimeter concern.

---

## Structured Error Object (all tools)

```json
{
  "error_type": "OptimisticConcurrencyError",
  "message": "Stream version mismatch on loan-app-123",
  "stream_id": "loan-app-123",
  "expected_version": 3,
  "actual_version": 5,
  "suggested_action": "reload_stream_and_retry"
}
```

All `error_type` values:
- `OptimisticConcurrencyError` → retry after reload
- `DomainError` → do not retry; fix the command
- `PreconditionFailed` → satisfy precondition first
- `ValidationError` → fix input data
- `StreamNotFoundError` → check stream_id
- `RateLimitedError` → wait and retry
- `ConcurrencyExhaustedError` → escalate to human

---

## Tool 1: `submit_application`

**Command**: `ApplicationSubmitted`
**Precondition**: None (first call in lifecycle)

**Input schema**:
```json
{
  "application_id": "string (UUID or business key)",
  "applicant_id": "string",
  "requested_amount_usd": "string (decimal)",
  "loan_purpose": "string",
  "submission_channel": "string ('web'|'api'|'branch')",
  "correlation_id": "string | null"
}
```

**Validation**: Pydantic `SubmitApplicationCommand`; duplicate `application_id`
check (expected_version=-1 on append enforces uniqueness).

**Return**:
```json
{
  "stream_id": "loan-app-123",
  "initial_version": 1
}
```

**Tool description for LLM**:
> Submits a new commercial loan application to The Ledger. Creates a new
> LoanApplication stream. If application_id already exists, returns
> OptimisticConcurrencyError. This is always the first tool called in the
> loan lifecycle.

---

## Tool 2: `start_agent_session`

**Command**: `AgentContextLoaded`
**Precondition**: None (but MUST be called before any agent decision tools)

**Input schema**:
```json
{
  "agent_id": "string",
  "session_id": "string (UUID recommended)",
  "context_source": "string ('event_replay'|'external_knowledge'|'hybrid')",
  "event_replay_from_position": "integer (default 0)",
  "context_token_count": "integer",
  "model_version": "string",
  "correlation_id": "string | null"
}
```

**Return**:
```json
{
  "session_id": "string",
  "context_position": "integer"
}
```

**Tool description for LLM**:
> Starts an agent work session and loads context into the AgentSession stream.
> MUST be called before record_credit_analysis, record_fraud_screening, or
> generate_decision. The session persists in the event store even across process
> restarts (Gas Town pattern). If an existing session exists for agent_id +
> session_id, returns the existing session's context_position.

---

## Tool 3: `record_credit_analysis`

**Command**: `CreditAnalysisCompleted`
**Precondition**: Active AgentSession for agent_id+session_id with context loaded.
loan stream must exist (submit_application called first).

**Input schema**:
```json
{
  "application_id": "string",
  "agent_id": "string",
  "session_id": "string",
  "model_version": "string",
  "confidence_score": "float (0.0–1.0)",
  "risk_tier": "string ('LOW'|'MEDIUM'|'HIGH'|'VERY_HIGH')",
  "recommended_limit_usd": "string (decimal)",
  "duration_ms": "integer",
  "input_data": "object (will be hashed)",
  "correlation_id": "string | null",
  "causation_id": "string | null"
}
```

**Validation**: Agent session active + context loaded; credit analysis not already
locked for this application; optimistic concurrency on loan stream.

**Return**:
```json
{
  "event_id": "UUID",
  "new_stream_version": "integer"
}
```

**Tool description for LLM**:
> Records a completed credit analysis for a loan application. Requires an active
> agent session created by start_agent_session — calling without an active session
> returns PreconditionFailed. If credit analysis has already been recorded for
> this application (and not overridden), returns DomainError with rule
> 'credit_analysis_locked'. Handles OptimisticConcurrencyError automatically
> with up to 3 retries.

---

## Tool 4: `record_fraud_screening`

**Command**: `FraudScreeningCompleted`
**Precondition**: Active AgentSession for agent_id+session_id with context loaded.

**Input schema**:
```json
{
  "application_id": "string",
  "agent_id": "string",
  "session_id": "string",
  "fraud_score": "float (0.0–1.0 validated)",
  "anomaly_flags": ["string"],
  "screening_model_version": "string",
  "input_data": "object",
  "correlation_id": "string | null",
  "causation_id": "string | null"
}
```

**Validation**: `fraud_score` must be in `[0.0, 1.0]`; agent session active +
context loaded.

**Return**:
```json
{
  "event_id": "UUID",
  "new_stream_version": "integer"
}
```

---

## Tool 5: `record_compliance_check`

**Command**: `ComplianceRulePassed` or `ComplianceRuleFailed`

**Input schema**:
```json
{
  "application_id": "string",
  "rule_id": "string",
  "rule_version": "string",
  "passed": "boolean",
  "evidence_hash": "string | null",
  "failure_reason": "string | null",
  "remediation_required": "boolean (default false)",
  "regulation_set_version": "string | null",
  "evaluation_timestamp": "string (ISO datetime) | null",
  "correlation_id": "string | null"
}
```

**Validation**: `rule_id` must be in the application's `checks_required` list
(loaded from ComplianceRecord stream).

**Return**:
```json
{
  "check_id": "UUID",
  "compliance_status": "PENDING | PASSED | FAILED"
}
```

---

## Tool 6: `generate_decision`

**Command**: `DecisionGenerated`
**Precondition**: Active AgentSession. All required agent analyses must be present
on the loan stream. confidence_floor < 0.6 forces REFER regardless of input.

**Input schema**:
```json
{
  "application_id": "string",
  "orchestrator_agent_id": "string",
  "recommendation": "string ('APPROVE'|'DECLINE'|'REFER')",
  "confidence_score": "float (0.0–1.0)",
  "contributing_agent_sessions": ["string (stream IDs)"],
  "decision_basis_summary": "string",
  "model_versions": {"agent_role": "model_version_string"},
  "correlation_id": "string | null",
  "causation_id": "string | null"
}
```

**Return**:
```json
{
  "decision_id": "UUID",
  "recommendation": "APPROVE | DECLINE | REFER"
}
```

**Tool description for LLM**:
> Generates the final AI recommendation for a loan application. Requires all
> agent analyses (credit, fraud) to be recorded first — calling before analyses
> are present returns PreconditionFailed. IMPORTANT: if confidence_score < 0.6,
> the recommendation is automatically overridden to 'REFER' regardless of input
> (regulatory requirement). contributing_agent_sessions must reference session
> stream IDs that actually processed this application_id — invalid references
> return DomainError with rule 'invalid_causal_chain'.

---

## Tool 7: `record_human_review`

**Command**: `HumanReviewCompleted`
**Precondition**: Application must be in ApprovedPendingHuman or
DeclinedPendingHuman state.

**Input schema**:
```json
{
  "application_id": "string",
  "reviewer_id": "string (caller-supplied; stored verbatim)",
  "override": "boolean",
  "final_decision": "string ('APPROVE'|'DECLINE')",
  "override_reason": "string | null (required if override=true)"
}
```

**Validation**: If `override=true`, `override_reason` must be non-empty (enforced
by aggregate). Application state must allow human review.

**Return**:
```json
{
  "final_decision": "APPROVE | DECLINE",
  "application_state": "FinalApproved | FinalDeclined"
}
```

---

## Tool 8: `run_integrity_check`

**Command**: `AuditIntegrityCheckRun`
**Precondition**: Rate-limited to 1 call per minute per entity (enforced by
AuditLedgerAggregate assertion).

**Input schema**:
```json
{
  "entity_type": "string",
  "entity_id": "string",
  "role": "string (caller-supplied; should be 'compliance')"
}
```

**Validation**: Rate limit enforced; `role` is stored in metadata verbatim
(no server-side role validation — perimeter concern).

**Return**:
```json
{
  "check_result": {
    "entity_type": "string",
    "entity_id": "string",
    "events_verified": "integer",
    "chain_valid": "boolean",
    "tamper_detected": "boolean",
    "integrity_hash": "string",
    "check_timestamp": "ISO datetime"
  },
  "chain_valid": "boolean"
}
```

**Tool description for LLM**:
> Runs a cryptographic audit chain integrity check on all events for the specified
> entity. Rate-limited to 1 call per minute per entity — calling more frequently
> returns RateLimitedError. Records the result as an AuditIntegrityCheckRun event
> in the AuditLedger stream. If tamper_detected=true, the chain is broken and a
> compliance incident should be escalated.
