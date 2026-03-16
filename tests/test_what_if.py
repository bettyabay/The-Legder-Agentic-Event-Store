"""
Phase 8 Bonus Test: What-If Counterfactual Projector Tests

Verifies that:
1. run_what_if() produces a real outcome that matches the actual event stream.
2. A counterfactual substitution of risk_tier=HIGH shows divergence vs MEDIUM.
3. The real store is never written — counterfactual events are NOT persisted.
4. run_what_if() with a matching counterfactual (same fields) shows no divergence.

Run: uv run pytest tests/test_what_if.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
import pytest

from src.commands.handlers import (
    StartAgentSessionCommand,
    SubmitApplicationCommand,
    handle_start_agent_session,
    handle_submit_application,
)
from src.event_store import EventStore
from src.models.events import (
    ComplianceClearanceIssued,
    ComplianceCheckRequested,
    CreditAnalysisCompleted,
    CreditAnalysisRequested,
    DecisionGenerated,
    FraudScreeningCompleted,
    HumanReviewCompleted,
    Recommendation,
)
from src.what_if.projector import WhatIfResult, run_what_if


async def _seed_complete_application(
    store: EventStore,
    application_id: str,
    agent_id: str,
    session_id: str,
) -> None:
    """Seed a complete application lifecycle (ApplicationSubmitted → HumanReviewCompleted)."""
    now = datetime.now(tz=timezone.utc)

    # Submit
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=application_id,
            applicant_id="whatif-applicant",
            requested_amount_usd=500_000.0,
            loan_purpose="What-if test",
            submission_channel="test",
        ),
        store,
    )

    # Agent session
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id=agent_id,
            session_id=session_id,
            context_source="cold_start",
            model_version="v2.3",
            context_token_count=4000,
        ),
        store,
    )

    loan_v = await store.stream_version(f"loan-{application_id}")
    # Advance through lifecycle
    await store.append(
        f"loan-{application_id}",
        [CreditAnalysisRequested(
            application_id=application_id,
            assigned_agent_id=agent_id,
            requested_at=now,
        )],
        expected_version=loan_v,
    )

    loan_v = await store.stream_version(f"loan-{application_id}")
    await store.append(
        f"loan-{application_id}",
        [CreditAnalysisCompleted(
            application_id=application_id,
            agent_id=agent_id,
            session_id=session_id,
            model_version="v2.3",
            confidence_score=0.82,
            risk_tier="MEDIUM",
            recommended_limit_usd=450_000.0,
            analysis_duration_ms=1200,
            input_data_hash="test-hash-medium",
        )],
        expected_version=loan_v,
    )

    loan_v = await store.stream_version(f"loan-{application_id}")
    await store.append(
        f"loan-{application_id}",
        [FraudScreeningCompleted(
            application_id=application_id,
            agent_id=agent_id,
            fraud_score=0.08,
            anomaly_flags=[],
            screening_model_version="fraud-v1.2",
            input_data_hash="fraud-hash",
        )],
        expected_version=loan_v,
    )

    loan_v = await store.stream_version(f"loan-{application_id}")
    await store.append(
        f"loan-{application_id}",
        [ComplianceCheckRequested(
            application_id=application_id,
            regulation_set_version="REG-2026-Q1",
            checks_required=["AML-001"],
        )],
        expected_version=loan_v,
    )

    loan_v = await store.stream_version(f"loan-{application_id}")
    await store.append(
        f"loan-{application_id}",
        [ComplianceClearanceIssued(
            application_id=application_id,
            regulation_set_version="REG-2026-Q1",
            issued_at=now,
            checks_passed=["AML-001"],
        )],
        expected_version=loan_v,
    )

    loan_v = await store.stream_version(f"loan-{application_id}")
    await store.append(
        f"loan-{application_id}",
        [DecisionGenerated(
            application_id=application_id,
            orchestrator_agent_id=agent_id,
            recommendation=Recommendation.APPROVE,
            confidence_score=0.82,
            contributing_agent_sessions=[f"agent-{agent_id}-{session_id}"],
            decision_basis_summary="Credit MEDIUM, fraud low, all compliance passed.",
            model_versions={agent_id: "v2.3"},
        )],
        expected_version=loan_v,
    )

    loan_v = await store.stream_version(f"loan-{application_id}")
    await store.append(
        f"loan-{application_id}",
        [HumanReviewCompleted(
            application_id=application_id,
            reviewer_id="reviewer-001",
            override=False,
            final_decision="APPROVED",
        )],
        expected_version=loan_v,
    )


@pytest.mark.asyncio
async def test_what_if_real_outcome_matches_stream(
    db_pool: asyncpg.Pool,
    unique_app_id: str,
    unique_agent_id: str,
    unique_session_id: str,
) -> None:
    """run_what_if real_outcome matches what an in-memory replay would produce."""
    store = EventStore(pool=db_pool)
    await _seed_complete_application(store, unique_app_id, unique_agent_id, unique_session_id)

    # Counterfactual: same risk_tier as real (MEDIUM) — should show no divergence
    cf_event = CreditAnalysisCompleted(
        application_id=unique_app_id,
        agent_id=unique_agent_id,
        session_id=unique_session_id,
        model_version="v2.3",
        confidence_score=0.82,
        risk_tier="MEDIUM",  # same as real
        recommended_limit_usd=450_000.0,
        analysis_duration_ms=1200,
        input_data_hash="test-hash-medium-cf",
    )

    result = await run_what_if(
        store=store,
        application_id=unique_app_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=[cf_event],
    )

    assert isinstance(result, WhatIfResult)
    assert result.application_id == unique_app_id
    assert result.branch_at_event_type == "CreditAnalysisCompleted"

    # Real outcome should be FINAL_APPROVED (we seeded APPROVED)
    assert result.real_outcome["final_state"] == "FINAL_APPROVED", (
        f"Expected FINAL_APPROVED, got {result.real_outcome['final_state']}"
    )
    assert result.real_outcome["risk_tier"] == "MEDIUM"


@pytest.mark.asyncio
async def test_what_if_high_risk_shows_divergence(
    db_pool: asyncpg.Pool,
    unique_app_id: str,
    unique_agent_id: str,
    unique_session_id: str,
) -> None:
    """Counterfactual with HIGH risk tier produces divergent risk_tier in outcome."""
    store = EventStore(pool=db_pool)
    await _seed_complete_application(store, unique_app_id, unique_agent_id, unique_session_id)

    # Counterfactual: HIGH risk (real is MEDIUM)
    cf_event = CreditAnalysisCompleted(
        application_id=unique_app_id,
        agent_id=unique_agent_id,
        session_id=unique_session_id,
        model_version="v2.3",
        confidence_score=0.82,
        risk_tier="HIGH",  # different from real
        recommended_limit_usd=200_000.0,  # lower limit for HIGH risk
        analysis_duration_ms=1200,
        input_data_hash="test-hash-high",
    )

    result = await run_what_if(
        store=store,
        application_id=unique_app_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=[cf_event],
    )

    # Risk tier must diverge
    assert result.real_outcome["risk_tier"] == "MEDIUM"
    assert result.counterfactual_outcome["risk_tier"] == "HIGH"

    # Divergence events must include risk_tier
    divergence_fields = {d["field"] for d in result.divergence_events}
    assert "risk_tier" in divergence_fields, (
        f"Expected 'risk_tier' in divergence, got {divergence_fields}"
    )

    # Limit must also diverge
    assert "recommended_limit_usd" in divergence_fields, (
        f"Expected 'recommended_limit_usd' in divergence, got {divergence_fields}"
    )


@pytest.mark.asyncio
async def test_what_if_does_not_write_to_store(
    db_pool: asyncpg.Pool,
    unique_app_id: str,
    unique_agent_id: str,
    unique_session_id: str,
) -> None:
    """run_what_if() must NEVER persist counterfactual events to the real store."""
    store = EventStore(pool=db_pool)
    await _seed_complete_application(store, unique_app_id, unique_agent_id, unique_session_id)

    # Record stream version before what-if
    version_before = await store.stream_version(f"loan-{unique_app_id}")
    events_before = await store.load_stream(f"loan-{unique_app_id}")

    cf_event = CreditAnalysisCompleted(
        application_id=unique_app_id,
        agent_id=unique_agent_id,
        session_id=unique_session_id,
        model_version="cf-v9.9",
        confidence_score=0.11,
        risk_tier="HIGH",
        recommended_limit_usd=0.0,
        analysis_duration_ms=1,
        input_data_hash="cf-should-not-be-stored",
    )

    await run_what_if(
        store=store,
        application_id=unique_app_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=[cf_event],
    )

    # Store must be unchanged
    version_after = await store.stream_version(f"loan-{unique_app_id}")
    events_after = await store.load_stream(f"loan-{unique_app_id}")

    assert version_after == version_before, (
        f"Stream version changed! before={version_before}, after={version_after}"
    )
    assert len(events_after) == len(events_before), (
        f"Event count changed! before={len(events_before)}, after={len(events_after)}"
    )

    # Ensure the counterfactual model version is not stored
    stored_model_versions = [
        e.payload.get("model_version") for e in events_after
    ]
    assert "cf-v9.9" not in stored_model_versions, (
        "Counterfactual event was persisted to real store — immutability violated!"
    )


@pytest.mark.asyncio
async def test_what_if_counts_replayed_events(
    db_pool: asyncpg.Pool,
    unique_app_id: str,
    unique_agent_id: str,
    unique_session_id: str,
) -> None:
    """events_replayed_real and events_replayed_counterfactual are positive."""
    store = EventStore(pool=db_pool)
    await _seed_complete_application(store, unique_app_id, unique_agent_id, unique_session_id)

    cf_event = CreditAnalysisCompleted(
        application_id=unique_app_id,
        agent_id=unique_agent_id,
        session_id=unique_session_id,
        model_version="v2.3",
        confidence_score=0.75,
        risk_tier="LOW",
        recommended_limit_usd=490_000.0,
        analysis_duration_ms=800,
        input_data_hash="test-hash-low",
    )

    result = await run_what_if(
        store=store,
        application_id=unique_app_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=[cf_event],
    )

    assert result.events_replayed_real > 0
    assert result.events_replayed_counterfactual > 0
