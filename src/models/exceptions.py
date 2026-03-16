"""
Domain exceptions for The Ledger.

All errors are typed so MCP tools can return structured error objects
that LLM consumers can reason about autonomously.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class LedgerError(Exception):
    """Base class for all Ledger errors."""


# ---------------------------------------------------------------------------
# Event Store Errors
# ---------------------------------------------------------------------------


@dataclass
class OptimisticConcurrencyError(LedgerError):
    """Raised when expected_version does not match the stream's actual version.

    The caller must reload the stream and retry its command.
    """

    stream_id: str
    expected_version: int
    actual_version: int
    suggested_action: str = field(
        default="reload_stream_and_retry",
        init=False,
    )
    error_type: str = field(default="OptimisticConcurrencyError", init=False)

    def __str__(self) -> str:
        return (
            f"Concurrency conflict on stream '{self.stream_id}': "
            f"expected version {self.expected_version}, "
            f"actual version {self.actual_version}. "
            f"Suggested action: {self.suggested_action}"
        )


@dataclass
class StreamNotFoundError(LedgerError):
    """Raised when a stream does not exist and was not expected to be new."""

    stream_id: str
    error_type: str = field(default="StreamNotFoundError", init=False)

    def __str__(self) -> str:
        return f"Stream '{self.stream_id}' does not exist."


@dataclass
class StreamArchivedError(LedgerError):
    """Raised when attempting to append to an archived stream."""

    stream_id: str
    error_type: str = field(default="StreamArchivedError", init=False)

    def __str__(self) -> str:
        return f"Stream '{self.stream_id}' is archived and cannot receive new events."


# ---------------------------------------------------------------------------
# Domain / Aggregate Errors
# ---------------------------------------------------------------------------


@dataclass
class DomainError(LedgerError):
    """Raised when a business invariant is violated inside an aggregate."""

    message: str
    aggregate_type: str = ""
    aggregate_id: str = ""
    error_type: str = field(default="DomainError", init=False)

    def __str__(self) -> str:
        prefix = (
            f"[{self.aggregate_type}:{self.aggregate_id}] "
            if self.aggregate_type
            else ""
        )
        return f"{prefix}{self.message}"


@dataclass
class InvalidStateTransitionError(DomainError):
    """Raised when an aggregate receives an event that is invalid for its current state."""

    current_state: str = ""
    attempted_event: str = ""
    error_type: str = field(default="InvalidStateTransitionError", init=False)

    def __post_init__(self) -> None:
        if not self.message:
            self.message = (
                f"Cannot apply '{self.attempted_event}' "
                f"in state '{self.current_state}'."
            )


@dataclass
class AgentContextNotLoadedError(DomainError):
    """Raised when an agent session attempts a decision before loading context.

    Gas Town invariant: AgentContextLoaded MUST be the first event in any session.
    """

    agent_id: str = ""
    session_id: str = ""
    error_type: str = field(default="AgentContextNotLoadedError", init=False)

    def __post_init__(self) -> None:
        if not self.message:
            self.message = (
                f"AgentSession '{self.agent_id}/{self.session_id}' "
                "has no AgentContextLoaded event. "
                "Call start_agent_session before any decision tool."
            )


@dataclass
class ComplianceClearanceBlockedError(DomainError):
    """Raised when ApplicationApproved is attempted with outstanding compliance checks."""

    missing_checks: list[str] = field(default_factory=list)
    error_type: str = field(default="ComplianceClearanceBlockedError", init=False)

    def __post_init__(self) -> None:
        if not self.message:
            self.message = (
                f"Cannot approve: mandatory compliance checks outstanding: "
                f"{self.missing_checks}"
            )


@dataclass
class ConfidenceFloorViolationError(DomainError):
    """Raised when confidence_score < 0.6 but recommendation != REFER."""

    confidence_score: float = 0.0
    recommendation: str = ""
    error_type: str = field(default="ConfidenceFloorViolationError", init=False)

    def __post_init__(self) -> None:
        if not self.message:
            self.message = (
                f"confidence_score={self.confidence_score} is below the 0.6 floor. "
                f"recommendation must be 'REFER', got '{self.recommendation}'."
            )


@dataclass
class InvalidCausalChainError(DomainError):
    """Raised when contributing_agent_sessions contains sessions with no decision for this app."""

    invalid_sessions: list[str] = field(default_factory=list)
    application_id: str = ""  # type: ignore[assignment]
    error_type: str = field(default="InvalidCausalChainError", init=False)

    def __post_init__(self) -> None:
        if not self.message:
            self.message = (
                f"contributing_agent_sessions contains sessions that never "
                f"processed application '{self.application_id}': "
                f"{self.invalid_sessions}"
            )


# ---------------------------------------------------------------------------
# MCP Tool Errors
# ---------------------------------------------------------------------------


@dataclass
class PreconditionFailedError(LedgerError):
    """Raised when an MCP tool's precondition is not satisfied."""

    tool_name: str
    precondition: str
    suggested_action: str = ""
    error_type: str = field(default="PreconditionFailedError", init=False)

    def __str__(self) -> str:
        return (
            f"Precondition for tool '{self.tool_name}' not met: {self.precondition}. "
            f"Suggested action: {self.suggested_action}"
        )


@dataclass
class RateLimitError(LedgerError):
    """Raised when an MCP tool is called more than its allowed rate."""

    tool_name: str
    limit_description: str
    error_type: str = field(default="RateLimitError", init=False)

    def __str__(self) -> str:
        return f"Rate limit exceeded for '{self.tool_name}': {self.limit_description}"


# ---------------------------------------------------------------------------
# Gas Town Pattern
# ---------------------------------------------------------------------------


@dataclass
class NeedsReconciliationError(LedgerError):
    """Raised when an agent context has a partial decision with no completion event.

    The agent must resolve the partial state before continuing.
    """

    agent_id: str
    session_id: str
    partial_event_type: str
    error_type: str = field(default="NeedsReconciliationError", init=False)

    def __str__(self) -> str:
        return (
            f"AgentSession '{self.agent_id}/{self.session_id}' has a partial decision "
            f"('{self.partial_event_type}' with no completion event). "
            "Reconcile before proceeding."
        )
