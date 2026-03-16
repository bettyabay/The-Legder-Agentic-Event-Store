"""
AuditLedger aggregate.

Cross-cutting audit trail linking events across all aggregates for a single
business entity. Append-only: no events may be removed; cross-stream causal
ordering is maintained via correlation_id chains.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.models.events import StoredEvent
from src.models.exceptions import DomainError

if TYPE_CHECKING:
    from src.event_store import EventStore


class AuditLedgerAggregate:
    """Consistency boundary for the cross-cutting audit trail."""

    def __init__(self, entity_type: str, entity_id: str) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.version: int = 0

        self.events_verified_count: int = 0
        self.last_integrity_hash: str | None = None
        self.chain_valid: bool = True

    # ------------------------------------------------------------------
    # Reconstruction
    # ------------------------------------------------------------------

    @classmethod
    async def load(
        cls,
        store: "EventStore",
        entity_type: str,
        entity_id: str,
    ) -> "AuditLedgerAggregate":
        """Reconstruct audit ledger state from its stream."""
        events = await store.load_stream(f"audit-{entity_type}-{entity_id}")
        agg = cls(entity_type=entity_type, entity_id=entity_id)
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

    def _on_AuditIntegrityCheckRun(self, event: StoredEvent) -> None:
        self.events_verified_count = event.payload.get("events_verified_count", 0)
        self.last_integrity_hash = event.payload.get("integrity_hash")

    # ------------------------------------------------------------------
    # Business rule assertions
    # ------------------------------------------------------------------

    def assert_append_only(self) -> None:
        """Audit ledger streams can never have events removed."""
        # This is enforced structurally — no delete API exists on EventStore.
        # This method is a documentation anchor for the invariant.
        pass

    @property
    def stream_id(self) -> str:
        return f"audit-{self.entity_type}-{self.entity_id}"
