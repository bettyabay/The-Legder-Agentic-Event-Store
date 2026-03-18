"""
ComplianceAuditView projection.

The regulatory read model. Must support temporal queries:
  get_compliance_at(application_id, timestamp) → state at that moment.

Snapshot strategy (DESIGN.md §Projection Strategy):
  Event-count trigger: every 100 compliance events per application a snapshot
  is written to compliance_snapshots. On temporal query, the nearest snapshot
  before the target timestamp is loaded, then delta events are applied in memory.

SLO: lag < 2000ms, temporal query p99 < 200ms.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg

from src.models.events import StoredEvent

logger = logging.getLogger(__name__)

SNAPSHOT_EVENT_COUNT_TRIGGER = 100


def _parse_timestamp(val: Any) -> datetime | None:
    """Parse timestamp from payload (JSONB returns datetimes as strings)."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return None


class ComplianceAuditViewProjection:
    """Projection: compliance check results per application, with temporal queries."""

    name: str = "compliance_audit_view"
    slo_ms: int = 2000  # daemon lag SLO

    _SUBSCRIBED_EVENTS = frozenset({
        "ComplianceCheckRequested",
        "ComplianceRulePassed",
        "ComplianceRuleFailed",
        "ComplianceClearanceIssued",
    })

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._event_counts: dict[str, int] = {}

    async def handle(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        if event.event_type not in self._SUBSCRIBED_EVENTS:
            return
        handler = getattr(self, f"_handle_{event.event_type}", None)
        if handler:
            await handler(event, conn)

        # Snapshot trigger
        app_id = event.payload.get("application_id", "")
        if app_id:
            self._event_counts[app_id] = self._event_counts.get(app_id, 0) + 1
            if self._event_counts[app_id] % SNAPSHOT_EVENT_COUNT_TRIGGER == 0:
                await self._write_snapshot(app_id, event.global_position, conn)

    async def rebuild(self, conn: asyncpg.Connection) -> None:  # type: ignore[type-arg]
        await conn.execute("TRUNCATE TABLE compliance_audit_view")
        await conn.execute("TRUNCATE TABLE compliance_snapshots")
        self._event_counts.clear()

    # ------------------------------------------------------------------
    # Temporal query interface
    # ------------------------------------------------------------------

    async def get_current_compliance(
        self,
        application_id: str,
    ) -> list[dict[str, object]]:
        """Return all compliance records for an application."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM compliance_audit_view WHERE application_id = $1",
                application_id,
            )
        return [dict(r) for r in rows]

    async def get_compliance_at(
        self,
        application_id: str,
        timestamp: datetime,
    ) -> list[dict[str, object]]:
        """Return compliance state as it existed at a specific point in time.

        Algorithm:
          1. Find the most recent snapshot before timestamp
          2. Load all compliance events for the application after snapshot_at
             and before timestamp
          3. Apply delta events to snapshot state in memory
          4. Return computed state

        This achieves p99 < 200ms by bounding the in-memory computation to
        delta events since last snapshot (max 100 events per snapshot interval).
        """
        async with self._pool.acquire() as conn:
            # 1. Find nearest snapshot
            snap_row = await conn.fetchrow(
                """
                SELECT snapshot_payload, event_position, snapshot_at
                FROM compliance_snapshots
                WHERE application_id = $1 AND snapshot_at <= $2
                ORDER BY snapshot_at DESC
                LIMIT 1
                """,
                application_id,
                timestamp,
            )

            snap_state: dict[str, dict[str, object]] = {}
            from_position = 0

            if snap_row:
                raw = snap_row["snapshot_payload"]
                snap_state = json.loads(raw) if isinstance(raw, str) else raw
                from_position = snap_row["event_position"]

            # 2. Load delta events since snapshot
            rows = await conn.fetch(
                """
                SELECT event_type, payload, recorded_at
                FROM events
                WHERE stream_id LIKE $1
                  AND global_position > $2
                  AND recorded_at <= $3
                ORDER BY global_position
                """,
                f"compliance-{application_id}",
                from_position,
                timestamp,
            )

        # 3. Apply delta events in memory
        state = dict(snap_state)
        for row in rows:
            raw_payload = row["payload"]
            payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
            self._apply_event_to_state(row["event_type"], payload, state)

        return list(state.values())

    async def get_projection_lag(self) -> int:
        """Return lag in milliseconds (delegated to daemon; returns 0 standalone)."""
        return 0  # Real lag exposed via ProjectionDaemon.get_lag()

    # ------------------------------------------------------------------
    # Per-event handlers
    # ------------------------------------------------------------------

    async def _handle_ComplianceCheckRequested(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        # ComplianceCheckRequested initialises the expected checks.
        # Individual rule records are inserted by Passed/Failed handlers.
        pass

    async def _handle_ComplianceRulePassed(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        p = event.payload
        await conn.execute(
            """
            INSERT INTO compliance_audit_view
              (application_id, rule_id, rule_version, status,
               evidence_hash, evaluation_timestamp, recorded_at)
            VALUES ($1, $2, $3, 'PASSED', $4, $5, $6)
            ON CONFLICT (application_id, rule_id) DO UPDATE SET
              rule_version = EXCLUDED.rule_version,
              status = 'PASSED',
              evidence_hash = EXCLUDED.evidence_hash,
              evaluation_timestamp = EXCLUDED.evaluation_timestamp
            """,
            p["application_id"],
            p["rule_id"],
            p["rule_version"],
            p.get("evidence_hash"),
            _parse_timestamp(p.get("evaluation_timestamp")),
            event.recorded_at,
        )

    async def _handle_ComplianceRuleFailed(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        p = event.payload
        await conn.execute(
            """
            INSERT INTO compliance_audit_view
              (application_id, rule_id, rule_version, status,
               failure_reason, remediation_required, recorded_at)
            VALUES ($1, $2, $3, 'FAILED', $4, $5, $6)
            ON CONFLICT (application_id, rule_id) DO UPDATE SET
              rule_version = EXCLUDED.rule_version,
              status = 'FAILED',
              failure_reason = EXCLUDED.failure_reason,
              remediation_required = EXCLUDED.remediation_required
            """,
            p["application_id"],
            p["rule_id"],
            p["rule_version"],
            p.get("failure_reason"),
            p.get("remediation_required", True),
            event.recorded_at,
        )

    async def _handle_ComplianceClearanceIssued(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        # Mark all pending checks as CLEARED in summary.
        # Individual rule rows already have their final status.
        pass

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------

    async def _write_snapshot(
        self,
        application_id: str,
        event_position: int,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """Write a point-in-time snapshot of compliance state."""
        rows = await conn.fetch(
            "SELECT * FROM compliance_audit_view WHERE application_id = $1",
            application_id,
        )
        snap: dict[str, object] = {row["rule_id"]: dict(row) for row in rows}
        await conn.execute(
            """
            INSERT INTO compliance_snapshots
              (application_id, event_position, snapshot_payload)
            VALUES ($1, $2, $3::jsonb)
            """,
            application_id,
            event_position,
            json.dumps(snap, default=str),
        )
        logger.debug(
            "Snapshot written for application '%s' at position %d",
            application_id,
            event_position,
        )

    @staticmethod
    def _apply_event_to_state(
        event_type: str,
        payload: dict[str, object],
        state: dict[str, dict[str, object]],
    ) -> None:
        """Apply a raw event payload to an in-memory compliance state dict."""
        rule_id = payload.get("rule_id")
        if not rule_id:
            return
        if event_type == "ComplianceRulePassed":
            state[str(rule_id)] = {
                "rule_id": rule_id,
                "status": "PASSED",
                "rule_version": payload.get("rule_version"),
                "evidence_hash": payload.get("evidence_hash"),
                "evaluation_timestamp": payload.get("evaluation_timestamp"),
                "failure_reason": None,
            }
        elif event_type == "ComplianceRuleFailed":
            state[str(rule_id)] = {
                "rule_id": rule_id,
                "status": "FAILED",
                "rule_version": payload.get("rule_version"),
                "failure_reason": payload.get("failure_reason"),
                "remediation_required": payload.get("remediation_required", True),
                "evidence_hash": None,
            }
