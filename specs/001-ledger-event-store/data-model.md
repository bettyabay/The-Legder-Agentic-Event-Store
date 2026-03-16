# Data Model: The Ledger — Agentic Event Store

**Branch**: `001-ledger-event-store` | **Date**: 2026-03-16

---

## 1. PostgreSQL Schema Tables

### 1.1 `events` — The Core Store

```sql
CREATE TABLE events (
  event_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  stream_id        TEXT NOT NULL,
  stream_position  BIGINT NOT NULL,            -- position within stream (1-based)
  global_position  BIGINT GENERATED ALWAYS AS IDENTITY,  -- global ordering across all streams
  event_type       TEXT NOT NULL,              -- e.g. "ApplicationSubmitted"
  event_version    SMALLINT NOT NULL DEFAULT 1,
  payload          JSONB NOT NULL,             -- event-type-specific data
  metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,  -- correlation_id, causation_id, etc.
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT uq_stream_position UNIQUE (stream_id, stream_position)
);

CREATE INDEX idx_events_stream_id ON events (stream_id, stream_position);
CREATE INDEX idx_events_global_pos ON events (global_position);
CREATE INDEX idx_events_type ON events (event_type);
CREATE INDEX idx_events_recorded ON events (recorded_at);
```

**Column justifications** (required in DESIGN.md):
- `event_id`: UUID primary key enables direct event lookup without stream context
- `stream_id`: text (not FK) for schema flexibility; format encodes aggregate type
- `stream_position`: optimistic concurrency lock key; enforced by `uq_stream_position`
- `global_position`: identity column provides total ordering for projection replay
- `event_type`: enables `load_all(event_types=[...])` filtering without full scan
- `event_version`: enables `UpcasterRegistry` chain lookup
- `payload`: JSONB for indexed querying of event data (e.g., `application_id` inside payload)
- `metadata`: correlation_id, causation_id, source_system — not domain data
- `recorded_at`: `clock_timestamp()` (not `now()`) captures wall time, not transaction time

### 1.2 `event_streams` — Stream Registry

```sql
CREATE TABLE event_streams (
  stream_id        TEXT PRIMARY KEY,
  aggregate_type   TEXT NOT NULL,              -- e.g. "LoanApplication"
  current_version  BIGINT NOT NULL DEFAULT 0,  -- optimistic concurrency version
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  archived_at      TIMESTAMPTZ,                -- NULL = active; set on archive_stream()
  metadata         JSONB NOT NULL DEFAULT '{}'::jsonb
);
```

**`aggregate_type` values**: `LoanApplication`, `AgentSession`, `ComplianceRecord`, `AuditLedger`

### 1.3 `projection_checkpoints` — Daemon State

```sql
CREATE TABLE projection_checkpoints (
  projection_name  TEXT PRIMARY KEY,           -- e.g. "application_summary"
  last_position    BIGINT NOT NULL DEFAULT 0,  -- last global_position processed
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 1.4 `outbox` — Guaranteed Delivery

```sql
CREATE TABLE outbox (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id         UUID NOT NULL REFERENCES events(event_id),
  destination      TEXT NOT NULL,              -- routing intent; e.g. "kafka-week-10"
  payload          JSONB NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  published_at     TIMESTAMPTZ,                -- NULL = unpublished
  attempts         SMALLINT NOT NULL DEFAULT 0
);
```

**Scope in this implementation**: outbox rows are written atomically with events
(FR-010); no outbox publisher process is implemented this week — that is Week 10.

### 1.5 `compliance_snapshots` — Temporal Query Support

```sql
CREATE TABLE compliance_snapshots (
  application_id    TEXT NOT NULL,
  snapshot_position BIGINT NOT NULL,           -- global_position of last included event
  snapshot_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  state_json        JSONB NOT NULL,            -- serialised ComplianceAuditView state
  PRIMARY KEY (application_id, snapshot_position)
);
CREATE INDEX idx_compliance_snapshots_app
  ON compliance_snapshots (application_id, snapshot_position DESC);
```

**Trigger**: taken every 100 compliance events per application (see research.md R-008).

### 1.6 `projection_errors` — Daemon Fault Record

```sql
CREATE TABLE projection_errors (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  projection_name  TEXT NOT NULL,
  event_id         UUID NOT NULL,
  global_position  BIGINT NOT NULL,
  error_message    TEXT NOT NULL,
  attempts         SMALLINT NOT NULL DEFAULT 1,
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

## 2. Pydantic Models

### 2.1 Base Types

```python
# src/models/events.py

class BaseEvent(BaseModel):
    """All domain events inherit from this. event_type auto-set to class name."""
    model_config = ConfigDict(frozen=True)       # immutable after construction
    event_type: ClassVar[str]                    # set by each subclass
    event_version: ClassVar[int] = 1

class StoredEvent(BaseModel):
    """Wrapper around a persisted event, including store metadata."""
    model_config = ConfigDict(frozen=True)
    event_id: UUID
    stream_id: str
    stream_position: int
    global_position: int
    event_type: str
    event_version: int
    payload: dict[str, Any]
    metadata: dict[str, Any]
    recorded_at: datetime

    def with_payload(self, new_payload: dict, version: int) -> "StoredEvent":
        """Return new StoredEvent with updated payload/version; original unchanged."""
        return self.model_copy(update={"payload": new_payload, "event_version": version})

class StreamMetadata(BaseModel):
    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime
    archived_at: datetime | None
    metadata: dict[str, Any]
```

### 2.2 Custom Exceptions

```python
class OptimisticConcurrencyError(Exception):
    def __init__(self, stream_id: str, expected_version: int, actual_version: int):
        self.stream_id = stream_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        self.suggested_action = "reload_stream_and_retry"

class ConcurrencyExhaustedError(Exception):
    """Raised after max_retries OptimisticConcurrencyErrors on the same operation."""
    def __init__(self, stream_id: str, attempts: int):
        self.stream_id = stream_id
        self.attempts = attempts

class DomainError(Exception):
    """Raised by aggregates when a business rule is violated."""
    def __init__(self, rule: str, message: str):
        self.rule = rule
        self.message = message

class StreamNotFoundError(Exception):
    def __init__(self, stream_id: str):
        self.stream_id = stream_id

class PreconditionFailed(Exception):
    """Raised by MCP tools when a precondition (e.g., active session) is unmet."""
    def __init__(self, precondition: str, suggested_action: str):
        self.precondition = precondition
        self.suggested_action = suggested_action
```

---

## 3. Event Catalogue — Complete Pydantic Models

### 3.1 LoanApplication Stream Events

```python
class ApplicationSubmitted(BaseEvent):
    event_type: ClassVar[str] = "ApplicationSubmitted"
    application_id: str
    applicant_id: str
    requested_amount_usd: Decimal
    loan_purpose: str
    submission_channel: str           # e.g. "web", "api", "branch"
    submitted_at: datetime

class CreditAnalysisRequested(BaseEvent):
    event_type: ClassVar[str] = "CreditAnalysisRequested"
    application_id: str
    assigned_agent_id: str
    requested_at: datetime
    priority: str                     # "HIGH" | "NORMAL" | "LOW"

class DecisionGenerated(BaseEvent):
    event_type: ClassVar[str] = "DecisionGenerated"
    event_version: ClassVar[int] = 2  # v1 had no model_versions{}
    application_id: str
    orchestrator_agent_id: str
    recommendation: str               # "APPROVE" | "DECLINE" | "REFER"
    confidence_score: float           # 0.0–1.0; < 0.6 forces REFER
    contributing_agent_sessions: list[str]  # stream IDs
    decision_basis_summary: str
    model_versions: dict[str, str]    # {"credit": "v2.3", "fraud": "v1.1", ...}

class HumanReviewCompleted(BaseEvent):
    event_type: ClassVar[str] = "HumanReviewCompleted"
    application_id: str
    reviewer_id: str                  # caller-supplied; no server-side validation
    override: bool
    final_decision: str               # "APPROVE" | "DECLINE"
    override_reason: str | None       # required if override=True

class ApplicationApproved(BaseEvent):
    event_type: ClassVar[str] = "ApplicationApproved"
    application_id: str
    approved_amount_usd: Decimal
    interest_rate: Decimal
    conditions: list[str]
    approved_by: str                  # human_id or "auto"
    effective_date: date

class ApplicationDeclined(BaseEvent):
    event_type: ClassVar[str] = "ApplicationDeclined"
    application_id: str
    decline_reasons: list[str]
    declined_by: str
    adverse_action_notice_required: bool
```

### 3.2 AgentSession Stream Events

```python
class AgentContextLoaded(BaseEvent):
    event_type: ClassVar[str] = "AgentContextLoaded"
    agent_id: str
    session_id: str
    context_source: str               # "event_replay" | "external_knowledge" | "hybrid"
    event_replay_from_position: int
    context_token_count: int
    model_version: str

class CreditAnalysisCompleted(BaseEvent):
    event_type: ClassVar[str] = "CreditAnalysisCompleted"
    event_version: ClassVar[int] = 2  # v1 had no model_version or confidence_score
    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: float | None    # None for v1 events upcasted from legacy
    risk_tier: str                    # "LOW" | "MEDIUM" | "HIGH" | "VERY_HIGH"
    recommended_limit_usd: Decimal
    analysis_duration_ms: int
    input_data_hash: str              # SHA-256 of input data

class FraudScreeningCompleted(BaseEvent):
    event_type: ClassVar[str] = "FraudScreeningCompleted"
    application_id: str
    agent_id: str
    fraud_score: float                # 0.0–1.0
    anomaly_flags: list[str]
    screening_model_version: str
    input_data_hash: str
```

### 3.3 ComplianceRecord Stream Events

```python
class ComplianceCheckRequested(BaseEvent):
    event_type: ClassVar[str] = "ComplianceCheckRequested"
    application_id: str
    regulation_set_version: str
    checks_required: list[str]        # rule IDs

class ComplianceRulePassed(BaseEvent):
    event_type: ClassVar[str] = "ComplianceRulePassed"
    application_id: str
    rule_id: str
    rule_version: str
    evaluation_timestamp: datetime
    evidence_hash: str

class ComplianceRuleFailed(BaseEvent):
    event_type: ClassVar[str] = "ComplianceRuleFailed"
    application_id: str
    rule_id: str
    rule_version: str
    failure_reason: str
    remediation_required: bool
```

### 3.4 AuditLedger Stream Events

```python
class AuditIntegrityCheckRun(BaseEvent):
    event_type: ClassVar[str] = "AuditIntegrityCheckRun"
    entity_id: str
    check_timestamp: datetime
    events_verified_count: int
    integrity_hash: str               # SHA-256 chain hash
    previous_hash: str                # previous AuditIntegrityCheckRun hash; "0"*64 for first
```

### 3.5 Missing Events (Phase 1 domain exercise — identified here)

The challenge states "the catalogue is intentionally incomplete." Additional
events required by the state machine and business rules:

```python
class FraudScreeningRequested(BaseEvent):
    """Required to trigger AgentSession for FraudDetection agent."""
    event_type: ClassVar[str] = "FraudScreeningRequested"
    application_id: str
    assigned_agent_id: str
    requested_at: datetime

class AnalysisCompleted(BaseEvent):
    """Signals that all agent analyses are complete; transitions to ComplianceReview."""
    event_type: ClassVar[str] = "AnalysisCompleted"
    application_id: str
    completed_at: datetime

class ComplianceClearanceGranted(BaseEvent):
    """Signals all required compliance rules have passed; enables ApprovedPendingHuman."""
    event_type: ClassVar[str] = "ComplianceClearanceGranted"
    application_id: str
    granted_at: datetime
    all_passed_rule_ids: list[str]

class HumanReviewOverride(BaseEvent):
    """Allows a second credit analysis after a human overrides the first decision."""
    event_type: ClassVar[str] = "HumanReviewOverride"
    application_id: str
    reviewer_id: str
    override_reason: str
    overridden_event_id: str          # event_id of the superseded CreditAnalysisCompleted
```

---

## 4. Aggregate State Types

### 4.1 `LoanApplicationAggregate`

```python
class ApplicationState(str, Enum):
    SUBMITTED               = "Submitted"
    AWAITING_ANALYSIS       = "AwaitingAnalysis"
    ANALYSIS_COMPLETE       = "AnalysisComplete"
    COMPLIANCE_REVIEW       = "ComplianceReview"
    PENDING_DECISION        = "PendingDecision"
    APPROVED_PENDING_HUMAN  = "ApprovedPendingHuman"
    DECLINED_PENDING_HUMAN  = "DeclinedPendingHuman"
    FINAL_APPROVED          = "FinalApproved"
    FINAL_DECLINED          = "FinalDeclined"

# Valid state machine transitions (enforced in _apply):
VALID_TRANSITIONS: dict[ApplicationState, set[ApplicationState]] = {
    ApplicationState.SUBMITTED:              {ApplicationState.AWAITING_ANALYSIS},
    ApplicationState.AWAITING_ANALYSIS:      {ApplicationState.ANALYSIS_COMPLETE},
    ApplicationState.ANALYSIS_COMPLETE:      {ApplicationState.COMPLIANCE_REVIEW},
    ApplicationState.COMPLIANCE_REVIEW:      {ApplicationState.PENDING_DECISION},
    ApplicationState.PENDING_DECISION:       {ApplicationState.APPROVED_PENDING_HUMAN,
                                              ApplicationState.DECLINED_PENDING_HUMAN},
    ApplicationState.APPROVED_PENDING_HUMAN: {ApplicationState.FINAL_APPROVED,
                                              ApplicationState.FINAL_DECLINED},
    ApplicationState.DECLINED_PENDING_HUMAN: {ApplicationState.FINAL_APPROVED,
                                              ApplicationState.FINAL_DECLINED},
}

class LoanApplicationAggregate:
    application_id: str
    state: ApplicationState
    version: int                      # = last stream_position
    applicant_id: str | None
    requested_amount: Decimal | None
    approved_amount: Decimal | None
    risk_tier: str | None
    confidence_score: float | None
    compliance_checks_required: list[str]
    compliance_checks_passed: list[str]
    credit_analysis_locked: bool      # True after first CreditAnalysisCompleted
    contributing_sessions: list[str]  # for causal chain validation
```

### 4.2 `AgentSessionAggregate`

```python
class SessionHealthStatus(str, Enum):
    ACTIVE               = "ACTIVE"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    COMPLETED            = "COMPLETED"

class AgentSessionAggregate:
    agent_id: str
    session_id: str
    version: int
    context_loaded: bool              # True after AgentContextLoaded event
    model_version: str | None
    completed_application_ids: set[str]
    health_status: SessionHealthStatus
```

### 4.3 `ComplianceRecordAggregate`

```python
class ComplianceRecordAggregate:
    application_id: str
    version: int
    regulation_set_version: str | None
    checks_required: list[str]
    checks_passed: list[str]
    checks_failed: list[str]          # with failure reasons
    clearance_granted: bool
```

### 4.4 `AuditLedgerAggregate`

```python
class AuditLedgerAggregate:
    entity_type: str
    entity_id: str
    version: int
    last_integrity_hash: str          # "0"*64 for genesis
    last_check_at: datetime | None
    last_check_rate_key: str          # for rate limiting: "audit-{entity_type}-{entity_id}"
```

---

## 5. Projection Schemas

### 5.1 `ApplicationSummary` Table

```sql
CREATE TABLE application_summary (
  application_id          TEXT PRIMARY KEY,
  state                   TEXT NOT NULL,
  applicant_id            TEXT,
  requested_amount_usd    NUMERIC(15,2),
  approved_amount_usd     NUMERIC(15,2),
  risk_tier               TEXT,
  fraud_score             NUMERIC(5,4),
  compliance_status       TEXT,        -- "PENDING" | "PASSED" | "FAILED"
  decision                TEXT,        -- "APPROVE" | "DECLINE" | "REFER" | NULL
  agent_sessions_completed TEXT[],     -- array of session stream IDs
  last_event_type         TEXT,
  last_event_at           TIMESTAMPTZ,
  human_reviewer_id       TEXT,
  final_decision_at       TIMESTAMPTZ
);
```

### 5.2 `AgentPerformanceLedger` Table

```sql
CREATE TABLE agent_performance_ledger (
  agent_id                TEXT NOT NULL,
  model_version           TEXT NOT NULL,
  analyses_completed      INTEGER NOT NULL DEFAULT 0,
  decisions_generated     INTEGER NOT NULL DEFAULT 0,
  avg_confidence_score    NUMERIC(5,4),
  avg_duration_ms         NUMERIC(10,2),
  approve_rate            NUMERIC(5,4),
  decline_rate            NUMERIC(5,4),
  refer_rate              NUMERIC(5,4),
  human_override_rate     NUMERIC(5,4),
  first_seen_at           TIMESTAMPTZ,
  last_seen_at            TIMESTAMPTZ,
  PRIMARY KEY (agent_id, model_version)
);
```

### 5.3 `ComplianceAuditView` Table

```sql
CREATE TABLE compliance_audit_view (
  application_id          TEXT PRIMARY KEY,
  regulation_set_version  TEXT,
  checks_required         TEXT[],
  checks_passed           JSONB,   -- [{rule_id, rule_version, evaluation_timestamp, evidence_hash}]
  checks_failed           JSONB,   -- [{rule_id, rule_version, failure_reason, remediation_required}]
  clearance_granted       BOOLEAN NOT NULL DEFAULT FALSE,
  last_event_at           TIMESTAMPTZ,
  last_global_position    BIGINT
);
```

---

## 6. Command Models (Pydantic)

```python
class SubmitApplicationCommand(BaseModel):
    application_id: str
    applicant_id: str
    requested_amount_usd: Decimal
    loan_purpose: str
    submission_channel: str
    correlation_id: str | None = None

class CreditAnalysisCompletedCommand(BaseModel):
    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: float
    risk_tier: str
    recommended_limit_usd: Decimal
    duration_ms: int
    input_data: dict                  # raw; hashed inside handler
    correlation_id: str | None = None
    causation_id: str | None = None

class FraudScreeningCompletedCommand(BaseModel):
    application_id: str
    agent_id: str
    session_id: str
    fraud_score: float                # validated 0.0–1.0
    anomaly_flags: list[str]
    screening_model_version: str
    input_data: dict
    correlation_id: str | None = None
    causation_id: str | None = None

class GenerateDecisionCommand(BaseModel):
    application_id: str
    orchestrator_agent_id: str
    recommendation: str
    confidence_score: float
    contributing_agent_sessions: list[str]
    decision_basis_summary: str
    model_versions: dict[str, str]
    correlation_id: str | None = None
    causation_id: str | None = None

class RecordHumanReviewCommand(BaseModel):
    application_id: str
    reviewer_id: str                  # caller-supplied; stored verbatim
    override: bool
    final_decision: str
    override_reason: str | None = None
    correlation_id: str | None = None

class StartAgentSessionCommand(BaseModel):
    agent_id: str
    session_id: str
    context_source: str
    event_replay_from_position: int = 0
    context_token_count: int
    model_version: str
    correlation_id: str | None = None

class RecordComplianceCheckCommand(BaseModel):
    application_id: str
    rule_id: str
    rule_version: str
    passed: bool
    evidence_hash: str | None = None
    failure_reason: str | None = None
    remediation_required: bool = False
    regulation_set_version: str | None = None
    evaluation_timestamp: datetime | None = None
    correlation_id: str | None = None
```

---

## 7. Output / Result Types

```python
class IntegrityCheckResult(BaseModel):
    entity_type: str
    entity_id: str
    events_verified: int
    chain_valid: bool
    tamper_detected: bool
    integrity_hash: str
    check_timestamp: datetime

class AgentContext(BaseModel):
    agent_id: str
    session_id: str
    context_text: str                 # prose summary within token_budget
    last_event_position: int
    pending_work: list[str]
    session_health_status: SessionHealthStatus

class WhatIfResult(BaseModel):
    application_id: str
    branch_event_type: str
    real_outcome: dict                # ApplicationSummary-like state
    counterfactual_outcome: dict
    divergence_events: list[str]     # event_types that differed
```

---

## 8. Entity Relationship Summary

```
event_streams (1) ──── (N) events
events (1) ──── (0-1) outbox
projection_checkpoints (1 per projection name)
compliance_snapshots (N per application_id)
projection_errors (N per projection + event)

application_summary (1 per application_id) ← built from LoanApplication + AgentSession + ComplianceRecord events
agent_performance_ledger (1 per agent_id+model_version) ← built from AgentSession events
compliance_audit_view (1 per application_id) ← built from ComplianceRecord events
compliance_snapshots (N per application_id) ← point-in-time snapshots of compliance_audit_view state
```
