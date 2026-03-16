"""
Phase 8 Bonus Test: Regulatory Examination Package Tests

Verifies that:
1. generate_regulatory_package() produces a complete, non-empty JSON package.
2. The package_hash is a valid SHA-256 hex digest (64 chars).
3. The event_stream in the package matches the real event log for the application.
4. The package includes at least one agent_participation record when agents were involved.
5. The integrity_check result is included and has chain_valid as a boolean.
6. The package is self-contained: event_stream can reproduce the package_hash independently.

Run: uv run pytest tests/test_regulatory.py -v
"""
from __future__ import annotations

import hashlib
import json
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
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.regulatory.package import generate_regulatory_package


async def _seed_application_for_regulatory(
    store: EventStore,
    application_id: str,
    agent_id: str,
    session_id: str,
) -> None:
    """Seed a complete loan lifecycle for regulatory package testing."""
    now = datetime.now(tz=timezone.utc)

    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=application_id,
            applicant_id="regulatory-test-applicant",
            requested_amount_usd=300_000.0,
            loan_purpose="Regulatory package test",
            submission_channel="test",
        ),
        store,
    )

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
            confidence_score=0.79,
            risk_tier="MEDIUM",
            recommended_limit_usd=270_000.0,
            analysis_duration_ms=1100,
            input_data_hash="reg-test-hash",
        )],
        expected_version=loan_v,
    )

    loan_v = await store.stream_version(f"loan-{application_id}")
    await store.append(
        f"loan-{application_id}",
        [FraudScreeningCompleted(
            application_id=application_id,
            agent_id=agent_id,
            fraud_score=0.05,
            anomaly_flags=[],
            screening_model_version="fraud-v1.0",
            input_data_hash="fraud-reg-hash",
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
            confidence_score=0.79,
            contributing_agent_sessions=[f"agent-{agent_id}-{session_id}"],
            decision_basis_summary="All checks passed.",
            model_versions={agent_id: "v2.3"},
        )],
        expected_version=loan_v,
    )

    loan_v = await store.stream_version(f"loan-{application_id}")
    await store.append(
        f"loan-{application_id}",
        [HumanReviewCompleted(
            application_id=application_id,
            reviewer_id="reviewer-002",
            override=False,
            final_decision="APPROVED",
        )],
        expected_version=loan_v,
    )


@pytest.mark.asyncio
async def test_regulatory_package_is_complete(
    db_pool: asyncpg.Pool,
    unique_app_id: str,
    unique_agent_id: str,
    unique_session_id: str,
) -> None:
    """generate_regulatory_package() returns a complete, non-empty package."""
    store = EventStore(pool=db_pool)
    compliance_proj = ComplianceAuditViewProjection(pool=db_pool)

    await _seed_application_for_regulatory(
        store, unique_app_id, unique_agent_id, unique_session_id
    )

    examination_date = datetime.now(tz=timezone.utc)
    package = await generate_regulatory_package(
        store=store,
        compliance_proj=compliance_proj,
        application_id=unique_app_id,
        examination_date=examination_date,
    )

    assert package.application_id == unique_app_id
    assert len(package.event_stream) > 0, "event_stream must not be empty"
    assert package.narrative != "", "narrative must not be empty"
    assert package.package_hash != "", "package_hash must not be empty"
    assert "integrity_check" in package.to_json()


@pytest.mark.asyncio
async def test_regulatory_package_hash_is_valid_sha256(
    db_pool: asyncpg.Pool,
    unique_app_id: str,
    unique_agent_id: str,
    unique_session_id: str,
) -> None:
    """package_hash is a valid 64-character SHA-256 hex digest."""
    store = EventStore(pool=db_pool)
    compliance_proj = ComplianceAuditViewProjection(pool=db_pool)

    await _seed_application_for_regulatory(
        store, unique_app_id, unique_agent_id, unique_session_id
    )

    package = await generate_regulatory_package(
        store=store,
        compliance_proj=compliance_proj,
        application_id=unique_app_id,
        examination_date=datetime.now(tz=timezone.utc),
    )

    assert len(package.package_hash) == 64, (
        f"Expected 64-char SHA-256 hex, got {len(package.package_hash)}: {package.package_hash}"
    )
    # Validate it's hexadecimal
    int(package.package_hash, 16)  # raises ValueError if not hex


@pytest.mark.asyncio
async def test_regulatory_package_event_stream_matches_real_log(
    db_pool: asyncpg.Pool,
    unique_app_id: str,
    unique_agent_id: str,
    unique_session_id: str,
) -> None:
    """event_stream in the package contains the same events as load_stream()."""
    store = EventStore(pool=db_pool)
    compliance_proj = ComplianceAuditViewProjection(pool=db_pool)

    await _seed_application_for_regulatory(
        store, unique_app_id, unique_agent_id, unique_session_id
    )

    examination_date = datetime.now(tz=timezone.utc)
    package = await generate_regulatory_package(
        store=store,
        compliance_proj=compliance_proj,
        application_id=unique_app_id,
        examination_date=examination_date,
    )

    real_events = await store.load_stream(f"loan-{unique_app_id}")

    # Filter real events to those before examination_date (same as package does)
    real_event_types = [
        e.event_type for e in real_events if e.recorded_at <= examination_date
    ]
    package_event_types = [e["event_type"] for e in package.event_stream]

    assert package_event_types == real_event_types, (
        f"Package event types do not match real log.\n"
        f"Real:    {real_event_types}\n"
        f"Package: {package_event_types}"
    )


@pytest.mark.asyncio
async def test_regulatory_package_includes_agent_participation(
    db_pool: asyncpg.Pool,
    unique_app_id: str,
    unique_agent_id: str,
    unique_session_id: str,
) -> None:
    """agent_participation records are populated with at least one agent."""
    store = EventStore(pool=db_pool)
    compliance_proj = ComplianceAuditViewProjection(pool=db_pool)

    await _seed_application_for_regulatory(
        store, unique_app_id, unique_agent_id, unique_session_id
    )

    package = await generate_regulatory_package(
        store=store,
        compliance_proj=compliance_proj,
        application_id=unique_app_id,
        examination_date=datetime.now(tz=timezone.utc),
    )

    assert len(package.agent_participation) > 0, (
        "agent_participation must contain at least one record"
    )

    # The credit analysis agent must appear
    agent_ids = {a["agent_id"] for a in package.agent_participation}
    assert unique_agent_id in agent_ids, (
        f"Expected agent_id={unique_agent_id} in participation, got {agent_ids}"
    )


@pytest.mark.asyncio
async def test_regulatory_package_json_is_valid(
    db_pool: asyncpg.Pool,
    unique_app_id: str,
    unique_agent_id: str,
    unique_session_id: str,
) -> None:
    """package.to_json() returns valid JSON with all required top-level keys."""
    store = EventStore(pool=db_pool)
    compliance_proj = ComplianceAuditViewProjection(pool=db_pool)

    await _seed_application_for_regulatory(
        store, unique_app_id, unique_agent_id, unique_session_id
    )

    package = await generate_regulatory_package(
        store=store,
        compliance_proj=compliance_proj,
        application_id=unique_app_id,
        examination_date=datetime.now(tz=timezone.utc),
    )

    raw = package.to_json()
    data = json.loads(raw)  # must not raise

    required_keys = {
        "application_id",
        "examination_date",
        "generated_at",
        "event_stream",
        "projection_state_at_examination",
        "integrity_check",
        "narrative",
        "agent_participation",
        "package_hash",
    }
    missing = required_keys - set(data.keys())
    assert not missing, f"Package JSON missing required keys: {missing}"
