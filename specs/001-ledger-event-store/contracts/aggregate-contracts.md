# Contract: Aggregate Interfaces

**Status**: FROZEN after Phase 2 completion (Constitution Principle III)
**Source**: Challenge document Phase 2
**Files**: `src/aggregates/*.py`

---

## Pattern Contract (All Aggregates)

Every aggregate MUST follow the `load → validate → determine → append` pattern.
No I/O is permitted inside `_apply` handlers or business rule assertion methods.

```python
@classmethod
async def load(cls, store: EventStore, *ids: str) -> "TAgg":
    """Replay event stream to reconstruct current state."""

def _apply(self, event: StoredEvent) -> None:
    """Dispatch to per-event-type handler; update version."""
    handler = getattr(self, f"_on_{event.event_type}", None)
    if handler:
        handler(event)
    self.version = event.stream_position
```

---

## `LoanApplicationAggregate` Contract

```python
class LoanApplicationAggregate:
    # State
    application_id: str
    state: ApplicationState
    version: int
    applicant_id: str | None
    requested_amount: Decimal | None
    approved_amount: Decimal | None
    risk_tier: str | None
    confidence_score: float | None
    compliance_checks_required: list[str]
    compliance_checks_passed: list[str]
    credit_analysis_locked: bool
    contributing_sessions: list[str]

    # Construction
    @classmethod
    async def load(cls, store: EventStore, application_id: str) -> "LoanApplicationAggregate": ...

    # Business rule assertions (raise DomainError on violation)
    def assert_state(self, expected: ApplicationState) -> None: ...
    def assert_awaiting_credit_analysis(self) -> None: ...
    def assert_analysis_not_locked(self) -> None: ...
    def assert_compliance_cleared(self) -> None: ...
    def assert_contributing_sessions_valid(self, session_ids: list[str]) -> None: ...
    def assert_can_approve(self) -> None: ...

    # Apply handlers (one per event type)
    def _on_ApplicationSubmitted(self, event: StoredEvent) -> None: ...
    def _on_CreditAnalysisRequested(self, event: StoredEvent) -> None: ...
    def _on_AnalysisCompleted(self, event: StoredEvent) -> None: ...
    def _on_DecisionGenerated(self, event: StoredEvent) -> None: ...
    def _on_HumanReviewCompleted(self, event: StoredEvent) -> None: ...
    def _on_ApplicationApproved(self, event: StoredEvent) -> None: ...
    def _on_ApplicationDeclined(self, event: StoredEvent) -> None: ...
    def _on_HumanReviewOverride(self, event: StoredEvent) -> None: ...
```

### Business Rules (enforced ONLY in aggregate assertions)

| Rule | Assertion Method | DomainError.rule |
|------|-----------------|------------------|
| BR-001 — State machine transitions | `assert_state` + `_apply` | `"invalid_state_transition"` |
| BR-002 — Credit analysis lock | `assert_analysis_not_locked` | `"credit_analysis_locked"` |
| BR-003 — Compliance dependency for approval | `assert_compliance_cleared` | `"compliance_not_cleared"` |
| BR-004 — Confidence floor | Checked in `_on_DecisionGenerated` before state update | `"confidence_floor_violated"` |
| BR-005 — Causal chain for contributing sessions | `assert_contributing_sessions_valid` | `"invalid_causal_chain"` |

---

## `AgentSessionAggregate` Contract

```python
class AgentSessionAggregate:
    # State
    agent_id: str
    session_id: str
    version: int
    context_loaded: bool
    model_version: str | None
    completed_application_ids: set[str]
    health_status: SessionHealthStatus

    # Construction
    @classmethod
    async def load(
        cls,
        store: EventStore,
        agent_id: str,
        session_id: str,
    ) -> "AgentSessionAggregate": ...

    # Business rule assertions
    def assert_context_loaded(self) -> None: ...          # BR-006
    def assert_model_version_current(self, model_version: str) -> None: ...  # BR-002 complement

    # Apply handlers
    def _on_AgentContextLoaded(self, event: StoredEvent) -> None: ...
    def _on_CreditAnalysisCompleted(self, event: StoredEvent) -> None: ...
    def _on_FraudScreeningCompleted(self, event: StoredEvent) -> None: ...
    def _on_DecisionGenerated(self, event: StoredEvent) -> None: ...
```

### Business Rules

| Rule | Assertion Method | DomainError.rule |
|------|-----------------|------------------|
| BR-006 — Agent context requirement (Gas Town) | `assert_context_loaded` | `"context_not_loaded"` |

---

## `ComplianceRecordAggregate` Contract

```python
class ComplianceRecordAggregate:
    application_id: str
    version: int
    regulation_set_version: str | None
    checks_required: list[str]
    checks_passed: list[str]
    checks_failed: list[str]
    clearance_granted: bool

    @classmethod
    async def load(
        cls,
        store: EventStore,
        application_id: str,
    ) -> "ComplianceRecordAggregate": ...

    def assert_all_checks_passed(self) -> None: ...
    def assert_rule_id_valid(self, rule_id: str) -> None: ...

    def _on_ComplianceCheckRequested(self, event: StoredEvent) -> None: ...
    def _on_ComplianceRulePassed(self, event: StoredEvent) -> None: ...
    def _on_ComplianceRuleFailed(self, event: StoredEvent) -> None: ...
    def _on_ComplianceClearanceGranted(self, event: StoredEvent) -> None: ...

    def is_cleared(self) -> bool:
        """True iff all checks_required are in checks_passed."""
```

---

## `AuditLedgerAggregate` Contract

```python
class AuditLedgerAggregate:
    entity_type: str
    entity_id: str
    version: int
    last_integrity_hash: str      # "0"*64 for genesis
    last_check_at: datetime | None

    @classmethod
    async def load(
        cls,
        store: EventStore,
        entity_type: str,
        entity_id: str,
    ) -> "AuditLedgerAggregate": ...

    def assert_rate_limit_not_exceeded(self) -> None:
        """Raises DomainError('rate_limited') if last_check_at was < 60s ago."""

    def _on_AuditIntegrityCheckRun(self, event: StoredEvent) -> None: ...
```

---

## What MUST NOT Be in Aggregates

- No `EventStore` I/O inside `_apply` or assertion methods
- No HTTP calls, no subprocess calls
- No Pydantic model construction (work with `event.payload` dict)
- No logging (aggregates are pure domain objects)
- No projection reads (aggregates load only their own stream)
