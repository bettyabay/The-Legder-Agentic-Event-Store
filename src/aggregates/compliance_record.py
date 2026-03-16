"""
ComplianceRecord aggregate.

Tracks regulatory checks, rule evaluations, and compliance verdicts for
each loan application. Separate from LoanApplication to prevent coupling
between credit-domain writes and compliance-domain writes under concurrent
scenarios (see DESIGN.md §Aggregate Boundary Justification).

Key invariant: cannot issue a compliance clearance without all mandatory
checks being present as ComplianceRulePassed events.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.models.events import StoredEvent
from src.models.exceptions import ComplianceClearanceBlockedError, DomainError

if TYPE_CHECKING:
    from src.event_store import EventStore


class ComplianceRecordAggregate:
    """Consistency boundary for compliance checks on a loan application."""

    def __init__(self, application_id: str) -> None:
        self.application_id = application_id
        self.version: int = 0

        self.regulation_set_version: str = ""
        self.required_checks: list[str] = []
        self.passed_checks: dict[str, str] = {}   # rule_id → rule_version
        self.failed_checks: dict[str, str] = {}   # rule_id → failure_reason
        self.clearance_issued: bool = False

    # ------------------------------------------------------------------
    # Reconstruction
    # ------------------------------------------------------------------

    @classmethod
    async def load(
        cls,
        store: "EventStore",
        application_id: str,
    ) -> "ComplianceRecordAggregate":
        """Reconstruct aggregate state from the compliance stream."""
        events = await store.load_stream(f"compliance-{application_id}")
        agg = cls(application_id=application_id)
        for event in events:
            agg._apply(event)
        return agg

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_ComplianceCheckRequested(self, event: StoredEvent) -> None:
        self.regulation_set_version = event.payload.get("regulation_set_version", "")
        self.required_checks = event.payload.get("checks_required", [])

    def _on_ComplianceRulePassed(self, event: StoredEvent) -> None:
        rule_id = event.payload["rule_id"]
        self.passed_checks[rule_id] = event.payload["rule_version"]
        # Remove from failed if previously failed and re-evaluated
        self.failed_checks.pop(rule_id, None)

    def _on_ComplianceRuleFailed(self, event: StoredEvent) -> None:
        rule_id = event.payload["rule_id"]
        self.failed_checks[rule_id] = event.payload["failure_reason"]

    def _on_ComplianceClearanceIssued(self, event: StoredEvent) -> None:
        self.clearance_issued = True

    # ------------------------------------------------------------------
    # Business rule assertions
    # ------------------------------------------------------------------

    def assert_all_required_checks_passed(self) -> None:
        """Cannot issue clearance without all required checks passing."""
        if not self.required_checks:
            raise DomainError(
                message="No compliance checks have been requested for this application.",
                aggregate_type="ComplianceRecord",
                aggregate_id=self.application_id,
            )
        missing = [
            c for c in self.required_checks if c not in self.passed_checks
        ]
        if missing:
            raise ComplianceClearanceBlockedError(
                message="",
                aggregate_type="ComplianceRecord",
                aggregate_id=self.application_id,
                missing_checks=missing,
            )
        if self.failed_checks:
            raise ComplianceClearanceBlockedError(
                message=(
                    f"Cannot issue clearance: {len(self.failed_checks)} "
                    f"checks have failed: {list(self.failed_checks)}"
                ),
                aggregate_type="ComplianceRecord",
                aggregate_id=self.application_id,
                missing_checks=list(self.failed_checks.keys()),
            )

    def assert_rule_in_regulation_set(self, rule_id: str) -> None:
        """Validate rule_id is part of the active regulation set."""
        if self.required_checks and rule_id not in self.required_checks:
            raise DomainError(
                message=(
                    f"rule_id '{rule_id}' is not in the active regulation set "
                    f"for this application."
                ),
                aggregate_type="ComplianceRecord",
                aggregate_id=self.application_id,
            )

    @property
    def stream_id(self) -> str:
        return f"compliance-{self.application_id}"
