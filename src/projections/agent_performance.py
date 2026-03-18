"""
AgentPerformanceLedger projection.

Aggregated performance metrics per AI agent model version.
SLO: reads p99 < 50ms (served directly from table, no computation needed).
"""
from __future__ import annotations

import logging

import asyncpg

from src.models.events import StoredEvent

logger = logging.getLogger(__name__)


class AgentPerformanceLedgerProjection:
    """Projection: metrics per (agent_id, model_version)."""

    name: str = "agent_performance_ledger"
    slo_ms: int = 500  # daemon lag SLO; read SLO is query-level < 50ms

    _SUBSCRIBED_EVENTS = frozenset({
        "AgentContextLoaded",
        "CreditAnalysisCompleted",
        "FraudScreeningCompleted",
        "DecisionGenerated",
        "HumanReviewCompleted",
    })

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

    async def rebuild(self, conn: asyncpg.Connection) -> None:  # type: ignore[type-arg]
        await conn.execute("TRUNCATE TABLE agent_performance_ledger")

    async def _ensure_row(
        self,
        agent_id: str,
        model_version: str,
        recorded_at: object,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        await conn.execute(
            """
            INSERT INTO agent_performance_ledger
              (agent_id, model_version, first_seen_at, last_seen_at)
            VALUES ($1, $2, $3, $3)
            ON CONFLICT (agent_id, model_version) DO UPDATE SET
              last_seen_at = GREATEST(agent_performance_ledger.last_seen_at, $3)
            """,
            agent_id,
            model_version,
            recorded_at,
        )

    async def _handle_AgentContextLoaded(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        p = event.payload
        await self._ensure_row(p["agent_id"], p["model_version"], event.recorded_at, conn)

    async def _handle_CreditAnalysisCompleted(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        p = event.payload
        agent_id = p["agent_id"]
        model_version = p.get("model_version", "unknown")
        duration = p.get("analysis_duration_ms")
        confidence = p.get("confidence_score")

        await self._ensure_row(agent_id, model_version, event.recorded_at, conn)

        duration_val = float(duration) if duration is not None else 0.0
        confidence_val: float | None = float(confidence) if confidence is not None else None

        if confidence_val is not None:
            await conn.execute(
                """
                UPDATE agent_performance_ledger SET
                  analyses_completed = analyses_completed + 1,
                  avg_duration_ms = CASE
                    WHEN avg_duration_ms IS NULL THEN $3
                    ELSE (avg_duration_ms * analyses_completed + $3) / (analyses_completed + 1)
                  END,
                  avg_confidence_score = CASE
                    WHEN avg_confidence_score IS NULL THEN $4
                    ELSE (avg_confidence_score * analyses_completed + $4) / (analyses_completed + 1)
                  END,
                  last_seen_at = $5
                WHERE agent_id = $1 AND model_version = $2
                """,
                agent_id,
                model_version,
                duration_val,
                confidence_val,
                event.recorded_at,
            )
        else:
            await conn.execute(
                """
                UPDATE agent_performance_ledger SET
                  analyses_completed = analyses_completed + 1,
                  avg_duration_ms = CASE
                    WHEN avg_duration_ms IS NULL THEN $3
                    ELSE (avg_duration_ms * analyses_completed + $3) / (analyses_completed + 1)
                  END,
                  last_seen_at = $4
                WHERE agent_id = $1 AND model_version = $2
                """,
                agent_id,
                model_version,
                duration_val,
                event.recorded_at,
            )

    async def _handle_DecisionGenerated(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        p = event.payload
        agent_id = p.get("orchestrator_agent_id", "")
        model_versions: dict[str, str] = p.get("model_versions", {})
        model_version = model_versions.get(agent_id, "unknown")
        recommendation = p.get("recommendation", "REFER")

        approve_inc = 1 if recommendation == "APPROVE" else 0
        decline_inc = 1 if recommendation == "DECLINE" else 0
        refer_inc = 1 if recommendation == "REFER" else 0

        await self._ensure_row(agent_id, model_version, event.recorded_at, conn)
        await conn.execute(
            """
            UPDATE agent_performance_ledger SET
              decisions_generated = decisions_generated + 1,
              approve_rate = (
                COALESCE(approve_rate, 0) * decisions_generated + $3
              ) / (decisions_generated + 1),
              decline_rate = (
                COALESCE(decline_rate, 0) * decisions_generated + $4
              ) / (decisions_generated + 1),
              refer_rate = (
                COALESCE(refer_rate, 0) * decisions_generated + $5
              ) / (decisions_generated + 1)
            WHERE agent_id = $1 AND model_version = $2
            """,
            agent_id,
            model_version,
            approve_inc,
            decline_inc,
            refer_inc,
        )

    async def _handle_HumanReviewCompleted(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """Track human override rate (needs join context; approximated here)."""
        # Human overrides are tracked at the application level.
        # Full override rate calculation is done in the regulatory package.
        pass
