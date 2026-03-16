"""
ApplicationSummary projection.

One row per loan application reflecting current state.
Updated by the ProjectionDaemon as events arrive.
SLO: lag < 500ms under normal load.
"""
from __future__ import annotations

import json
import logging

import asyncpg

from src.models.events import StoredEvent

logger = logging.getLogger(__name__)


class ApplicationSummaryProjection:
    """Projection: one row per loan application, current state."""

    name: str = "application_summary"
    slo_ms: int = 500  # SLO: p99 lag < 500ms

    _SUBSCRIBED_EVENTS = frozenset({
        "ApplicationSubmitted",
        "CreditAnalysisRequested",
        "CreditAnalysisCompleted",
        "FraudScreeningCompleted",
        "ComplianceCheckRequested",
        "ComplianceRulePassed",
        "ComplianceRuleFailed",
        "ComplianceClearanceIssued",
        "DecisionGenerated",
        "HumanReviewCompleted",
        "ApplicationApproved",
        "ApplicationDeclined",
    })

    async def handle(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """Process a single event; idempotent via upsert."""
        if event.event_type not in self._SUBSCRIBED_EVENTS:
            return

        handler = getattr(self, f"_handle_{event.event_type}", None)
        if handler:
            await handler(event, conn)

    async def rebuild(self, conn: asyncpg.Connection) -> None:  # type: ignore[type-arg]
        """Truncate projection; daemon will replay from position 0."""
        await conn.execute("TRUNCATE TABLE application_summary")

    # ------------------------------------------------------------------
    # Per-event handlers
    # ------------------------------------------------------------------

    async def _handle_ApplicationSubmitted(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        p = event.payload
        await conn.execute(
            """
            INSERT INTO application_summary
              (application_id, state, applicant_id, requested_amount_usd,
               last_event_type, last_event_at, updated_at)
            VALUES ($1, 'SUBMITTED', $2, $3, $4, $5, NOW())
            ON CONFLICT (application_id) DO UPDATE SET
              state = 'SUBMITTED',
              applicant_id = EXCLUDED.applicant_id,
              requested_amount_usd = EXCLUDED.requested_amount_usd,
              last_event_type = EXCLUDED.last_event_type,
              last_event_at = EXCLUDED.last_event_at,
              updated_at = NOW()
            """,
            p["application_id"],
            p["applicant_id"],
            p["requested_amount_usd"],
            event.event_type,
            event.recorded_at,
        )

    async def _handle_CreditAnalysisCompleted(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        p = event.payload
        app_id = p["application_id"]
        session_ref = f"agent-{p['agent_id']}-{p['session_id']}"

        await conn.execute(
            """
            UPDATE application_summary SET
              state = 'ANALYSIS_COMPLETE',
              risk_tier = $2,
              agent_sessions_completed = (
                  SELECT jsonb_agg(DISTINCT elem)
                  FROM jsonb_array_elements_text(
                      COALESCE(agent_sessions_completed, '[]'::jsonb) || $3::jsonb
                  ) elem
              ),
              last_event_type = $4,
              last_event_at = $5,
              updated_at = NOW()
            WHERE application_id = $1
            """,
            app_id,
            p.get("risk_tier"),
            json.dumps([session_ref]),
            event.event_type,
            event.recorded_at,
        )

    async def _handle_FraudScreeningCompleted(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        p = event.payload
        await conn.execute(
            """
            UPDATE application_summary SET
              fraud_score = $2,
              last_event_type = $3,
              last_event_at = $4,
              updated_at = NOW()
            WHERE application_id = $1
            """,
            p["application_id"],
            p.get("fraud_score"),
            event.event_type,
            event.recorded_at,
        )

    async def _handle_ComplianceClearanceIssued(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        p = event.payload
        await conn.execute(
            """
            UPDATE application_summary SET
              compliance_status = 'CLEARED',
              state = 'PENDING_DECISION',
              last_event_type = $2,
              last_event_at = $3,
              updated_at = NOW()
            WHERE application_id = $1
            """,
            p["application_id"],
            event.event_type,
            event.recorded_at,
        )

    async def _handle_DecisionGenerated(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        p = event.payload
        rec = p.get("recommendation", "")
        state = "APPROVED_PENDING_HUMAN" if rec == "APPROVE" else "DECLINED_PENDING_HUMAN"
        await conn.execute(
            """
            UPDATE application_summary SET
              state = $2,
              decision = $3,
              last_event_type = $4,
              last_event_at = $5,
              updated_at = NOW()
            WHERE application_id = $1
            """,
            p["application_id"],
            state,
            rec,
            event.event_type,
            event.recorded_at,
        )

    async def _handle_HumanReviewCompleted(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        p = event.payload
        final = p.get("final_decision", "")
        state = "FINAL_APPROVED" if final in ("APPROVED", "APPROVE") else "FINAL_DECLINED"
        await conn.execute(
            """
            UPDATE application_summary SET
              state = $2,
              human_reviewer_id = $3,
              final_decision_at = $4,
              last_event_type = $5,
              last_event_at = $4,
              updated_at = NOW()
            WHERE application_id = $1
            """,
            p["application_id"],
            state,
            p.get("reviewer_id"),
            event.recorded_at,
            event.event_type,
        )

    async def _handle_ApplicationApproved(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        p = event.payload
        await conn.execute(
            """
            UPDATE application_summary SET
              state = 'FINAL_APPROVED',
              approved_amount_usd = $2,
              last_event_type = $3,
              last_event_at = $4,
              updated_at = NOW()
            WHERE application_id = $1
            """,
            p["application_id"],
            p.get("approved_amount_usd"),
            event.event_type,
            event.recorded_at,
        )

    async def _handle_ApplicationDeclined(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        p = event.payload
        await conn.execute(
            """
            UPDATE application_summary SET
              state = 'FINAL_DECLINED',
              last_event_type = $2,
              last_event_at = $3,
              updated_at = NOW()
            WHERE application_id = $1
            """,
            p["application_id"],
            event.event_type,
            event.recorded_at,
        )

    # Generic fallback for state-tracking events
    async def _handle_generic(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
        state: str | None = None,
    ) -> None:
        updates: list[str] = ["last_event_type = $2", "last_event_at = $3", "updated_at = NOW()"]
        params: list[object] = [event.payload.get("application_id"), event.event_type, event.recorded_at]
        if state:
            updates.append(f"state = ${len(params) + 1}")
            params.append(state)
        sql = f"UPDATE application_summary SET {', '.join(updates)} WHERE application_id = $1"
        await conn.execute(sql, *params)

    _handle_CreditAnalysisRequested = lambda self, e, c: self._handle_generic(e, c, "AWAITING_ANALYSIS")  # noqa: E731
    _handle_ComplianceCheckRequested = lambda self, e, c: self._handle_generic(e, c, "COMPLIANCE_REVIEW")  # noqa: E731
    _handle_ComplianceRulePassed = lambda self, e, c: self._handle_generic(e, c)  # noqa: E731
    _handle_ComplianceRuleFailed = lambda self, e, c: self._handle_generic(e, c)  # noqa: E731
