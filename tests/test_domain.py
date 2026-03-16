"""
Phase 4 Mandatory Test: Business Rules Enforcement Tests

Tests all 6 business rules (BR-1 through BR-6) with invalid inputs.
Each test exercises aggregate assertions directly — no MCP layer, no handlers
(except where a full integration path is needed to reach the guarded state).

Business Rules:
  BR-1: State machine guard — InvalidStateTransitionError on invalid transition
  BR-2: Gas Town invariant — AgentContextNotLoadedError if no context loaded
  BR-3: Credit analysis locking — DomainError if second analysis attempted
  BR-4: Confidence floor — ConfidenceFloorViolationError if APPROVE with score < 0.6
  BR-5: Compliance clearance — ComplianceClearanceBlockedError without clearance
  BR-6: Causal chain — InvalidCausalChainError if session never processed this app

Run: uv run pytest tests/test_domain.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
import pytest

from src.aggregates.agent_session import AgentSessionAggregate
from src.aggregates.loan_application import LoanApplicationAggregate
from src.commands.handlers import (
    CreditAnalysisCompletedCommand,
    StartAgentSessionCommand,
    SubmitApplicationCommand,
    handle_credit_analysis_completed,
    handle_start_agent_session,
    handle_submit_application,
)
from src.event_store import EventStore
from src.models.events import (
    ApplicationState,
    CreditAnalysisRequested,
)
from src.models.exceptions import (
    AgentContextNotLoadedError,
    ComplianceClearanceBlockedError,
    ConfidenceFloorViolationError,
    DomainError,
    InvalidCausalChainError,
    InvalidStateTransitionError,
)

# ──────────────────────────────────────────────────────────────────────────────
# BR-1: State Machine Guard
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_br1_invalid_state_transition_on_wrong_state(
    db_pool: asyncpg.Pool,
    unique_app_id: str,
) -> None:
    """BR-1: assert_awaiting_credit_analysis() on a SUBMITTED app raises InvalidStateTransitionError."""
    store = EventStore(pool=db_pool)

    # Submit — state is SUBMITTED, not AWAITING_ANALYSIS
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=unique_app_id,
            applicant_id="br1-applicant",
            requested_amount_usd=100_000.0,
            loan_purpose="BR-1 test",
            submission_channel="test",
        ),
        store,
    )

    app = await LoanApplicationAggregate.load(store, unique_app_id)
    assert app.state == ApplicationState.SUBMITTED

    # Attempting a credit-analysis guard on SUBMITTED state must fail
    with pytest.raises(InvalidStateTransitionError) as exc_info:
        app.assert_awaiting_credit_analysis()

    err = exc_info.value
    assert err.error_type == "InvalidStateTransitionError"
    assert "SUBMITTED" in str(err)


def test_br1_state_machine_allows_valid_transition() -> None:
    """BR-1: assert_awaiting_credit_analysis() succeeds when state is AWAITING_ANALYSIS."""
    app = LoanApplicationAggregate(application_id="test-br1-valid")
    app.state = ApplicationState.AWAITING_ANALYSIS
    # Should not raise
    app.assert_awaiting_credit_analysis()


# ──────────────────────────────────────────────────────────────────────────────
# BR-2: Gas Town Invariant
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_br2_context_not_loaded_raises_on_credit_analysis(
    db_pool: asyncpg.Pool,
    unique_app_id: str,
    unique_agent_id: str,
    unique_session_id: str,
) -> None:
    """BR-2: Credit analysis attempted without start_agent_session raises AgentContextNotLoadedError."""
    store = EventStore(pool=db_pool)

    # Submit + advance to AWAITING_ANALYSIS
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=unique_app_id,
            applicant_id="br2-applicant",
            requested_amount_usd=150_000.0,
            loan_purpose="BR-2 test",
            submission_channel="test",
        ),
        store,
    )
    await store.append(
        f"loan-{unique_app_id}",
        [
            CreditAnalysisRequested(
                application_id=unique_app_id,
                assigned_agent_id=unique_agent_id,
                requested_at=datetime.now(tz=timezone.utc),
            )
        ],
        expected_version=1,
    )

    # Attempt credit analysis WITHOUT calling start_agent_session first
    with pytest.raises(AgentContextNotLoadedError) as exc_info:
        await handle_credit_analysis_completed(
            CreditAnalysisCompletedCommand(
                application_id=unique_app_id,
                agent_id=unique_agent_id,
                session_id=unique_session_id,  # session stream does NOT exist
                model_version="v2.3",
                confidence_score=0.80,
                risk_tier="MEDIUM",
                recommended_limit_usd=130_000.0,
                duration_ms=800,
                input_data={"test": "br2"},
            ),
            store,
        )

    err = exc_info.value
    assert err.error_type == "AgentContextNotLoadedError"


def test_br2_context_loaded_flag_raises_when_false() -> None:
    """BR-2: AgentSessionAggregate.assert_context_loaded() raises when not loaded."""
    agent = AgentSessionAggregate(agent_id="test-agent", session_id="test-session")
    assert agent.context_loaded is False

    with pytest.raises(AgentContextNotLoadedError) as exc_info:
        agent.assert_context_loaded()

    assert exc_info.value.error_type == "AgentContextNotLoadedError"


def test_br2_context_loaded_flag_passes_when_true() -> None:
    """BR-2: assert_context_loaded() succeeds after context is marked loaded."""
    agent = AgentSessionAggregate(agent_id="test-agent", session_id="test-session")
    agent.context_loaded = True
    # Should not raise
    agent.assert_context_loaded()


# ──────────────────────────────────────────────────────────────────────────────
# BR-3: Credit Analysis Locking
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_br3_credit_analysis_locked_on_second_attempt(
    db_pool: asyncpg.Pool,
    unique_app_id: str,
    unique_agent_id: str,
    unique_session_id: str,
) -> None:
    """BR-3: Second CreditAnalysisCompleted on same application raises DomainError."""
    store = EventStore(pool=db_pool)

    # Setup: submit + session + first credit analysis
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=unique_app_id,
            applicant_id="br3-applicant",
            requested_amount_usd=200_000.0,
            loan_purpose="BR-3 test",
            submission_channel="test",
        ),
        store,
    )
    await store.append(
        f"loan-{unique_app_id}",
        [
            CreditAnalysisRequested(
                application_id=unique_app_id,
                assigned_agent_id=unique_agent_id,
                requested_at=datetime.now(tz=timezone.utc),
            )
        ],
        expected_version=1,
    )
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id=unique_agent_id,
            session_id=unique_session_id,
            context_source="cold_start",
            model_version="v2.3",
            context_token_count=3000,
        ),
        store,
    )
    # First credit analysis — succeeds
    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=unique_app_id,
            agent_id=unique_agent_id,
            session_id=unique_session_id,
            model_version="v2.3",
            confidence_score=0.78,
            risk_tier="MEDIUM",
            recommended_limit_usd=180_000.0,
            duration_ms=900,
            input_data={"attempt": 1},
        ),
        store,
    )

    # Verify the lock was set
    app = await LoanApplicationAggregate.load(store, unique_app_id)
    assert app.credit_analysis_completed is True

    # Directly test the lock assertion
    with pytest.raises(DomainError) as exc_info:
        app.assert_credit_analysis_not_locked()

    assert isinstance(exc_info.value, DomainError)


def test_br3_lock_not_set_on_fresh_aggregate() -> None:
    """BR-3: Fresh aggregate has no lock; assert_credit_analysis_not_locked() passes."""
    app = LoanApplicationAggregate(application_id="test-br3-fresh")
    assert app.credit_analysis_completed is False
    # Should not raise
    app.assert_credit_analysis_not_locked()


# ──────────────────────────────────────────────────────────────────────────────
# BR-4: Confidence Floor
# ──────────────────────────────────────────────────────────────────────────────


def test_br4_confidence_floor_violated_on_approve() -> None:
    """BR-4: APPROVE with confidence_score < 0.6 raises ConfidenceFloorViolationError."""
    app = LoanApplicationAggregate(application_id="test-br4-floor")

    with pytest.raises(ConfidenceFloorViolationError) as exc_info:
        app.assert_confidence_floor(confidence_score=0.45, recommendation="APPROVE")

    err = exc_info.value
    assert err.error_type == "ConfidenceFloorViolationError"
    assert err.confidence_score == 0.45
    assert err.recommendation == "APPROVE"


def test_br4_confidence_floor_violated_on_decline() -> None:
    """BR-4: DECLINE with confidence_score < 0.6 also raises (only REFER is allowed)."""
    app = LoanApplicationAggregate(application_id="test-br4-decline")

    with pytest.raises(ConfidenceFloorViolationError):
        app.assert_confidence_floor(confidence_score=0.59, recommendation="DECLINE")


def test_br4_confidence_floor_refer_always_allowed() -> None:
    """BR-4: REFER with any confidence_score does not raise."""
    app = LoanApplicationAggregate(application_id="test-br4-refer")
    # Even with very low confidence, REFER is always legal
    app.assert_confidence_floor(confidence_score=0.01, recommendation="REFER")
    app.assert_confidence_floor(confidence_score=0.59, recommendation="REFER")


def test_br4_confidence_at_boundary_allows_approve() -> None:
    """BR-4: confidence_score exactly 0.6 allows APPROVE (boundary is exclusive)."""
    app = LoanApplicationAggregate(application_id="test-br4-boundary")
    # 0.6 is NOT below the floor (< 0.6 is the condition)
    app.assert_confidence_floor(confidence_score=0.60, recommendation="APPROVE")


# ──────────────────────────────────────────────────────────────────────────────
# BR-5: Compliance Clearance Dependency
# ──────────────────────────────────────────────────────────────────────────────


def test_br5_compliance_not_cleared_raises() -> None:
    """BR-5: Decision without compliance clearance raises ComplianceClearanceBlockedError."""
    app = LoanApplicationAggregate(application_id="test-br5-blocked")
    app.compliance_clearance_issued = False
    app.required_compliance_checks = ["AML-001", "KYC-002"]
    app.completed_compliance_checks = ["AML-001"]  # KYC-002 missing

    with pytest.raises(ComplianceClearanceBlockedError) as exc_info:
        app.assert_compliance_clearance()

    err = exc_info.value
    assert err.error_type == "ComplianceClearanceBlockedError"


def test_br5_no_checks_requested_also_blocked() -> None:
    """BR-5: Approval with zero checks requested raises ComplianceClearanceBlockedError."""
    app = LoanApplicationAggregate(application_id="test-br5-empty")
    app.compliance_clearance_issued = False
    app.required_compliance_checks = []
    app.completed_compliance_checks = []

    with pytest.raises(ComplianceClearanceBlockedError):
        app.assert_compliance_clearance()


def test_br5_clearance_issued_allows_decision() -> None:
    """BR-5: compliance_clearance_issued=True bypasses all check assertions."""
    app = LoanApplicationAggregate(application_id="test-br5-cleared")
    app.compliance_clearance_issued = True
    # Should not raise even with empty checks (clearance is the authority)
    app.assert_compliance_clearance()


# ──────────────────────────────────────────────────────────────────────────────
# BR-6: Causal Chain Enforcement
# ──────────────────────────────────────────────────────────────────────────────


def test_br6_invalid_causal_chain_raises() -> None:
    """BR-6: contributing_agent_sessions referencing a non-participant raises InvalidCausalChainError."""
    app = LoanApplicationAggregate(application_id="test-br6-chain")

    sessions_with_decisions = {"agent-valid-session-001"}

    with pytest.raises(InvalidCausalChainError) as exc_info:
        app.assert_causal_chain(
            contributing_sessions=["agent-valid-session-001", "agent-ghost-session-999"],
            sessions_with_decisions=sessions_with_decisions,
        )

    err = exc_info.value
    assert err.error_type == "InvalidCausalChainError"
    assert "agent-ghost-session-999" in err.invalid_sessions


def test_br6_all_sessions_valid_passes() -> None:
    """BR-6: All contributing sessions have processed this app — no error."""
    app = LoanApplicationAggregate(application_id="test-br6-valid")

    sessions_with_decisions = {"agent-session-001", "agent-session-002"}

    # Should not raise
    app.assert_causal_chain(
        contributing_sessions=["agent-session-001", "agent-session-002"],
        sessions_with_decisions=sessions_with_decisions,
    )


def test_br6_empty_contributing_sessions_passes() -> None:
    """BR-6: Empty contributing_sessions list raises no error (nothing to validate)."""
    app = LoanApplicationAggregate(application_id="test-br6-empty")
    # Should not raise
    app.assert_causal_chain(
        contributing_sessions=[],
        sessions_with_decisions=set(),
    )
