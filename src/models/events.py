"""
Pydantic v2 event models for The Ledger.

All events inherit from BaseEvent. The full event catalogue is defined here
plus the StoredEvent wrapper returned by load_stream() and load_all().

Version constants are co-located with each event class so upcasters can
import them without coupling to the registry.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Base types
# ---------------------------------------------------------------------------


class BaseEvent(BaseModel):
    """Abstract base for all domain events."""

    model_config = {"frozen": True}

    event_type: str
    event_version: int = 1

    def payload_dict(self) -> dict[str, Any]:
        """Return the event payload as a plain dict (excludes event_type / version)."""
        return self.model_dump(exclude={"event_type", "event_version"})


class StoredEvent(BaseModel):
    """Event as returned from the event store — enriched with store metadata."""

    model_config = {"frozen": True}

    event_id: UUID
    stream_id: str
    stream_position: int
    global_position: int
    event_type: str
    event_version: int
    payload: dict[str, Any]
    metadata: dict[str, Any]
    recorded_at: datetime

    def with_payload(
        self,
        new_payload: dict[str, Any],
        version: int,
    ) -> "StoredEvent":
        """Return a new StoredEvent with updated payload and version (for upcasting)."""
        return self.model_copy(update={"payload": new_payload, "event_version": version})


class StreamMetadata(BaseModel):
    """Metadata about an event stream (from event_streams table)."""

    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime
    archived_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Application state machine
# ---------------------------------------------------------------------------


class ApplicationState(str, Enum):
    SUBMITTED = "SUBMITTED"
    AWAITING_ANALYSIS = "AWAITING_ANALYSIS"
    ANALYSIS_COMPLETE = "ANALYSIS_COMPLETE"
    COMPLIANCE_REVIEW = "COMPLIANCE_REVIEW"
    PENDING_DECISION = "PENDING_DECISION"
    APPROVED_PENDING_HUMAN = "APPROVED_PENDING_HUMAN"
    DECLINED_PENDING_HUMAN = "DECLINED_PENDING_HUMAN"
    FINAL_APPROVED = "FINAL_APPROVED"
    FINAL_DECLINED = "FINAL_DECLINED"


class Recommendation(str, Enum):
    APPROVE = "APPROVE"
    DECLINE = "DECLINE"
    REFER = "REFER"


class SessionHealth(str, Enum):
    OK = "OK"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    TERMINATED = "TERMINATED"


# ---------------------------------------------------------------------------
# LoanApplication events
# ---------------------------------------------------------------------------


class ApplicationSubmitted(BaseEvent):
    event_type: str = "ApplicationSubmitted"
    event_version: int = 1

    application_id: str
    applicant_id: str
    requested_amount_usd: float
    loan_purpose: str
    submission_channel: str
    submitted_at: datetime


class CreditAnalysisRequested(BaseEvent):
    event_type: str = "CreditAnalysisRequested"
    event_version: int = 1

    application_id: str
    assigned_agent_id: str
    requested_at: datetime
    priority: str = "NORMAL"


class DecisionGenerated(BaseEvent):
    """Version 2 includes model_versions dict.  Version 1 is upcasted at read time."""

    event_type: str = "DecisionGenerated"
    event_version: int = 2

    application_id: str
    orchestrator_agent_id: str
    recommendation: Recommendation
    confidence_score: float
    contributing_agent_sessions: list[str]
    decision_basis_summary: str
    model_versions: dict[str, str] = Field(default_factory=dict)

    @field_validator("confidence_score")
    @classmethod
    def check_confidence_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"confidence_score must be in [0, 1], got {v}")
        return v


class HumanReviewCompleted(BaseEvent):
    event_type: str = "HumanReviewCompleted"
    event_version: int = 1

    application_id: str
    reviewer_id: str
    override: bool
    final_decision: str
    override_reason: str | None = None


class ApplicationApproved(BaseEvent):
    event_type: str = "ApplicationApproved"
    event_version: int = 1

    application_id: str
    approved_amount_usd: float
    interest_rate: float
    conditions: list[str] = Field(default_factory=list)
    approved_by: str  # human_id or "auto"
    effective_date: datetime


class ApplicationDeclined(BaseEvent):
    event_type: str = "ApplicationDeclined"
    event_version: int = 1

    application_id: str
    decline_reasons: list[str]
    declined_by: str
    adverse_action_notice_required: bool = True


# ---------------------------------------------------------------------------
# AgentSession events
# ---------------------------------------------------------------------------


class AgentContextLoaded(BaseEvent):
    """Gas Town invariant: first event in every AgentSession stream."""

    event_type: str = "AgentContextLoaded"
    event_version: int = 1

    agent_id: str
    session_id: str
    context_source: str
    event_replay_from_position: int = 0
    context_token_count: int
    model_version: str


class CreditAnalysisCompleted(BaseEvent):
    """Version 2 adds model_version, confidence_score, regulatory_basis.
    Version 1 events are upcasted at read time by CreditAnalysisV1ToV2Upcaster."""

    event_type: str = "CreditAnalysisCompleted"
    event_version: int = 2

    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: float | None
    risk_tier: str
    recommended_limit_usd: float
    analysis_duration_ms: int
    input_data_hash: str
    regulatory_basis: str | None = None


class FraudScreeningCompleted(BaseEvent):
    event_type: str = "FraudScreeningCompleted"
    event_version: int = 1

    application_id: str
    agent_id: str
    fraud_score: float
    anomaly_flags: list[str] = Field(default_factory=list)
    screening_model_version: str
    input_data_hash: str

    @field_validator("fraud_score")
    @classmethod
    def check_fraud_score_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"fraud_score must be in [0, 1], got {v}")
        return v


# ---------------------------------------------------------------------------
# ComplianceRecord events
# ---------------------------------------------------------------------------


class ComplianceCheckRequested(BaseEvent):
    event_type: str = "ComplianceCheckRequested"
    event_version: int = 1

    application_id: str
    regulation_set_version: str
    checks_required: list[str]


class ComplianceRulePassed(BaseEvent):
    event_type: str = "ComplianceRulePassed"
    event_version: int = 1

    application_id: str
    rule_id: str
    rule_version: str
    evaluation_timestamp: datetime
    evidence_hash: str


class ComplianceRuleFailed(BaseEvent):
    event_type: str = "ComplianceRuleFailed"
    event_version: int = 1

    application_id: str
    rule_id: str
    rule_version: str
    failure_reason: str
    remediation_required: bool = True


class ComplianceClearanceIssued(BaseEvent):
    """Emitted when all mandatory checks are passed for an application."""

    event_type: str = "ComplianceClearanceIssued"
    event_version: int = 1

    application_id: str
    regulation_set_version: str
    issued_at: datetime
    checks_passed: list[str]


# ---------------------------------------------------------------------------
# AuditLedger events
# ---------------------------------------------------------------------------


class AuditIntegrityCheckRun(BaseEvent):
    event_type: str = "AuditIntegrityCheckRun"
    event_version: int = 1

    entity_id: str
    check_timestamp: datetime
    events_verified_count: int
    integrity_hash: str
    previous_hash: str | None = None  # None for first check in chain


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def hash_inputs(data: Any) -> str:
    """Return SHA-256 hex digest of JSON-serialised data (deterministic)."""
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def make_event_id() -> UUID:
    return uuid4()


# ---------------------------------------------------------------------------
# Week-5 canonical event catalogue (single source of truth)
# ---------------------------------------------------------------------------
#
# The repo includes the Week-5 canonical event models in
# `starter/ledger/schema/events.py`. We re-export them here so the rest of the
# `src/` codebase can import event types from a single place.
#
# Note: `StoredEvent` / `StreamMetadata` / `BaseEvent` wrappers for the DB are
# intentionally kept in this module. The canonical catalogue defines *domain*
# event classes whose instances are serialised via `.to_store_dict()`.
#
# By importing at the end, these re-exports override the reduced placeholders
# defined earlier in this file.
