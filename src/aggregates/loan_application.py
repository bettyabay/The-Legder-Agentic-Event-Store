"""
LoanApplication aggregate.

Tracks the full lifecycle of a commercial loan application from submission
to final decision. Enforces all 6 business rules defined in the challenge.

State machine:
    Submitted → AwaitingAnalysis → AnalysisComplete → ComplianceReview →
    PendingDecision → ApprovedPendingHuman / DeclinedPendingHuman →
    FinalApproved / FinalDeclined

Business rules enforced in this aggregate (never in API/MCP layer):
  BR-1  Valid state machine transitions only — DomainError on invalid transition
  BR-3  Model version locking — one CreditAnalysisCompleted per application
  BR-4  Confidence floor — DecisionGenerated with score < 0.6 must be REFER
  BR-5  Compliance dependency — ApprovedPendingHuman only if all checks pass
  BR-6  Causal chain enforcement — contributing_agent_sessions validation
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.models.events import (
    ApplicationApproved,
    ApplicationDeclined,
    ApplicationState,
    ApplicationSubmitted,
    ComplianceClearanceIssued,
    CreditAnalysisCompleted,
    CreditAnalysisRequested,
    DecisionGenerated,
    FraudScreeningCompleted,
    HumanReviewCompleted,
    Recommendation,
    StoredEvent,
)
from src.models.exceptions import (
    ComplianceClearanceBlockedError,
    ConfidenceFloorViolationError,
    DomainError,
    InvalidCausalChainError,
    InvalidStateTransitionError,
)

if TYPE_CHECKING:
    from src.event_store import EventStore

# Valid state transitions — (from_state, event_type) → to_state
_TRANSITIONS: dict[tuple[ApplicationState, str], ApplicationState] = {
    (ApplicationState.SUBMITTED, "CreditAnalysisRequested"): ApplicationState.AWAITING_ANALYSIS,
    (ApplicationState.AWAITING_ANALYSIS, "CreditAnalysisCompleted"): ApplicationState.ANALYSIS_COMPLETE,
    (ApplicationState.ANALYSIS_COMPLETE, "FraudScreeningCompleted"): ApplicationState.ANALYSIS_COMPLETE,
    (ApplicationState.ANALYSIS_COMPLETE, "ComplianceCheckRequested"): ApplicationState.COMPLIANCE_REVIEW,
    (ApplicationState.COMPLIANCE_REVIEW, "ComplianceRulePassed"): ApplicationState.COMPLIANCE_REVIEW,
    (ApplicationState.COMPLIANCE_REVIEW, "ComplianceRuleFailed"): ApplicationState.COMPLIANCE_REVIEW,
    (ApplicationState.COMPLIANCE_REVIEW, "ComplianceClearanceIssued"): ApplicationState.PENDING_DECISION,
    (ApplicationState.PENDING_DECISION, "DecisionGenerated"): ApplicationState.APPROVED_PENDING_HUMAN,
    (ApplicationState.APPROVED_PENDING_HUMAN, "HumanReviewCompleted"): ApplicationState.FINAL_APPROVED,
    (ApplicationState.DECLINED_PENDING_HUMAN, "HumanReviewCompleted"): ApplicationState.FINAL_DECLINED,
}

# Events that move to DECLINED path
_DECLINE_EVENTS = frozenset({"ApplicationDeclined"})

# Events that do NOT change state (idempotent applications)
_STATELESS_EVENTS = frozenset({
    "AuditIntegrityCheckRun",
    "ComplianceRulePassed",
    "ComplianceRuleFailed",
})


class LoanApplicationAggregate:
    """Consistency boundary for the LoanApplication domain object."""

    def __init__(self, application_id: str) -> None:
        self.application_id = application_id
        self.state: ApplicationState | None = None
        self.version: int = 0

        # Tracked fields for business rule enforcement
        self.applicant_id: str = ""
        self.requested_amount: float = 0.0
        self.approved_amount: float | None = None
        self.credit_analysis_completed: bool = False
        self.compliance_clearance_issued: bool = False
        self.required_compliance_checks: list[str] = []
        self.completed_compliance_checks: list[str] = []
        self.contributing_agent_sessions: list[str] = []

    # ------------------------------------------------------------------
    # Reconstruction
    # ------------------------------------------------------------------

    @classmethod
    async def load(
        cls,
        store: "EventStore",
        application_id: str,
    ) -> "LoanApplicationAggregate":
        """Reconstruct aggregate state by replaying its event stream."""
        events = await store.load_stream(f"loan-{application_id}")
        agg = cls(application_id=application_id)
        for event in events:
            agg._apply(event)
        return agg

    def _apply(self, event: StoredEvent) -> None:
        """Route event to its handler and advance version."""
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position

    # ------------------------------------------------------------------
    # Event handlers (one per event type)
    # ------------------------------------------------------------------

    def _on_ApplicationSubmitted(self, event: StoredEvent) -> None:
        self.state = ApplicationState.SUBMITTED
        self.applicant_id = event.payload["applicant_id"]
        self.requested_amount = event.payload["requested_amount_usd"]

    def _on_CreditAnalysisRequested(self, event: StoredEvent) -> None:
        self.state = ApplicationState.AWAITING_ANALYSIS

    def _on_CreditAnalysisCompleted(self, event: StoredEvent) -> None:
        self.state = ApplicationState.ANALYSIS_COMPLETE
        self.credit_analysis_completed = True

    def _on_FraudScreeningCompleted(self, event: StoredEvent) -> None:
        # Fraud screening does not change the primary state machine
        pass

    def _on_ComplianceCheckRequested(self, event: StoredEvent) -> None:
        self.state = ApplicationState.COMPLIANCE_REVIEW
        self.required_compliance_checks = event.payload.get("checks_required", [])

    def _on_ComplianceRulePassed(self, event: StoredEvent) -> None:
        rule_id = event.payload["rule_id"]
        if rule_id not in self.completed_compliance_checks:
            self.completed_compliance_checks.append(rule_id)

    def _on_ComplianceRuleFailed(self, event: StoredEvent) -> None:
        pass  # Compliance failures tracked in ComplianceRecord aggregate

    def _on_ComplianceClearanceIssued(self, event: StoredEvent) -> None:
        self.state = ApplicationState.PENDING_DECISION
        self.compliance_clearance_issued = True

    def _on_DecisionGenerated(self, event: StoredEvent) -> None:
        recommendation = event.payload["recommendation"]
        if recommendation in (Recommendation.APPROVE, "APPROVE"):
            self.state = ApplicationState.APPROVED_PENDING_HUMAN
        else:
            self.state = ApplicationState.DECLINED_PENDING_HUMAN
        self.contributing_agent_sessions = event.payload.get(
            "contributing_agent_sessions", []
        )

    def _on_HumanReviewCompleted(self, event: StoredEvent) -> None:
        final = event.payload["final_decision"]
        if final in ("APPROVED", "APPROVE"):
            self.state = ApplicationState.FINAL_APPROVED
        else:
            self.state = ApplicationState.FINAL_DECLINED

    def _on_ApplicationApproved(self, event: StoredEvent) -> None:
        self.state = ApplicationState.FINAL_APPROVED
        self.approved_amount = event.payload["approved_amount_usd"]

    def _on_ApplicationDeclined(self, event: StoredEvent) -> None:
        self.state = ApplicationState.FINAL_DECLINED

    # ------------------------------------------------------------------
    # Business rule assertions (called by command handlers)
    # ------------------------------------------------------------------

    def assert_in_state(self, *allowed_states: ApplicationState) -> None:
        """BR-1: State machine guard — raise if current state is not in allowed_states."""
        if self.state not in allowed_states:
            raise InvalidStateTransitionError(
                message="",
                aggregate_type="LoanApplication",
                aggregate_id=self.application_id,
                current_state=str(self.state),
                attempted_event=", ".join(str(s) for s in allowed_states),
            )

    def assert_awaiting_credit_analysis(self) -> None:
        """Guard for credit analysis command handlers."""
        self.assert_in_state(ApplicationState.AWAITING_ANALYSIS)

    def assert_awaiting_fraud_screening(self) -> None:
        self.assert_in_state(
            ApplicationState.AWAITING_ANALYSIS,
            ApplicationState.ANALYSIS_COMPLETE,
        )

    def assert_awaiting_compliance_check(self) -> None:
        self.assert_in_state(ApplicationState.COMPLIANCE_REVIEW)

    def assert_awaiting_decision(self) -> None:
        self.assert_in_state(ApplicationState.PENDING_DECISION)

    def assert_awaiting_human_review(self) -> None:
        self.assert_in_state(
            ApplicationState.APPROVED_PENDING_HUMAN,
            ApplicationState.DECLINED_PENDING_HUMAN,
        )

    def assert_credit_analysis_not_locked(self) -> None:
        """BR-3: Model version locking — only one credit analysis per application."""
        if self.credit_analysis_completed:
            raise DomainError(
                message=(
                    "Credit analysis already completed for this application. "
                    "Cannot append a second CreditAnalysisCompleted without "
                    "a HumanReviewOverride event."
                ),
                aggregate_type="LoanApplication",
                aggregate_id=self.application_id,
            )

    def assert_confidence_floor(
        self,
        confidence_score: float,
        recommendation: str,
    ) -> None:
        """BR-4: confidence_score < 0.6 MUST produce recommendation = REFER."""
        if confidence_score < 0.6 and recommendation != "REFER":
            raise ConfidenceFloorViolationError(
                message="",
                aggregate_type="LoanApplication",
                aggregate_id=self.application_id,
                confidence_score=confidence_score,
                recommendation=recommendation,
            )

    def assert_compliance_clearance(self) -> None:
        """BR-5: All required compliance checks must be complete before approval."""
        if not self.compliance_clearance_issued:
            missing = [
                c
                for c in self.required_compliance_checks
                if c not in self.completed_compliance_checks
            ]
            raise ComplianceClearanceBlockedError(
                message="",
                aggregate_type="LoanApplication",
                aggregate_id=self.application_id,
                missing_checks=missing,
            )

    def assert_causal_chain(
        self,
        contributing_sessions: list[str],
        sessions_with_decisions: set[str],
    ) -> None:
        """BR-6: Every contributing session must have produced a decision for this app."""
        invalid = [s for s in contributing_sessions if s not in sessions_with_decisions]
        if invalid:
            raise InvalidCausalChainError(
                message="",
                aggregate_type="LoanApplication",
                aggregate_id=self.application_id,
                invalid_sessions=invalid,
                application_id=self.application_id,
            )
