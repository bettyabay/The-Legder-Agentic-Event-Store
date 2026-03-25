"""
MCP Resources — The Query Side of The Ledger.

Resources expose projections. They NEVER load aggregate streams directly
(except the two justified exceptions documented below).

SLOs per resource URI:
  ledger://applications/{id}                 p99 < 50ms   (ApplicationSummary table)
  ledger://applications/{id}/compliance      p99 < 200ms  (ComplianceAuditView + snapshots)
  ledger://applications/{id}/audit-trail     p99 < 500ms  (AuditLedger direct — justified exception)
  ledger://agents/{id}/performance           p99 < 50ms   (AgentPerformanceLedger table)
  ledger://agents/{id}/sessions/{session_id} p99 < 300ms  (AgentSession direct — justified exception)
  ledger://ledger/health                     p99 < 10ms   (ProjectionDaemon.get_all_lags())

Justified exceptions to "no stream reads in resources":
  1. audit-trail: The AuditLedger stream IS the read model for compliance examination.
     A separate projection table would duplicate it without benefit.
  2. agent sessions: Session streams are small (bounded by session lifetime) and
     the session-level temporal query requires full event detail.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import asyncpg
from mcp.server import Server
from mcp.types import Resource, TextContent

from src.event_store import EventStore
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon

logger = logging.getLogger(__name__)


def _normalize_uri(uri: object) -> str:
    """Coerce MCP URI input to plain string.

    Some MCP clients pass pydantic AnyUrl objects instead of raw str.
    """
    return unquote(str(uri))


def register_resources(
    server: Server,
    store: EventStore,
    pool: asyncpg.Pool,
    daemon: ProjectionDaemon,
    compliance_proj: ComplianceAuditViewProjection,
) -> None:
    """Register all 6 MCP resources on the server instance."""

    @server.list_resources()
    async def list_resources() -> list[Resource]:
        return [
            Resource(
                uri="ledger://applications/{id}",
                name="Application Summary",
                description="Current state of a loan application (ApplicationSummary projection). p99 < 50ms.",
                mimeType="application/json",
            ),
            Resource(
                uri="ledger://applications/{id}/compliance",
                name="Compliance Audit View",
                description=(
                    "Full compliance record for an application. "
                    "Supports temporal queries: append ?as_of=ISO8601_TIMESTAMP for point-in-time state. "
                    "SLO: p99 < 200ms."
                ),
                mimeType="application/json",
            ),
            Resource(
                uri="ledger://applications/{id}/audit-trail",
                name="Audit Trail",
                description=(
                    "Complete ordered event stream for an application (AuditLedger). "
                    "Supports range queries: ?from=ISO8601&to=ISO8601. "
                    "SLO: p99 < 500ms."
                ),
                mimeType="application/json",
            ),
            Resource(
                uri="ledger://agents/{id}/performance",
                name="Agent Performance Ledger",
                description="Aggregated performance metrics per agent model version. p99 < 50ms.",
                mimeType="application/json",
            ),
            Resource(
                uri="ledger://agents/{id}/sessions/{session_id}",
                name="Agent Session",
                description="Full event stream for a specific agent session. Supports full replay. p99 < 300ms.",
                mimeType="application/json",
            ),
            Resource(
                uri="ledger://ledger/health",
                name="Ledger Health",
                description="ProjectionDaemon lag for all projections. Watchdog endpoint. p99 < 10ms.",
                mimeType="application/json",
            ),
        ]

    @server.read_resource()
    async def read_resource(uri: object) -> str:
        uri_str = _normalize_uri(uri)
        try:
            return await _dispatch_resource(uri_str, store, pool, daemon, compliance_proj)
        except Exception as exc:
            logger.exception("Error reading resource '%s'", uri_str)
            return json.dumps({"error": str(exc), "error_type": type(exc).__name__})


async def _dispatch_resource(
    uri: str,
    store: EventStore,
    pool: asyncpg.Pool,
    daemon: ProjectionDaemon,
    compliance_proj: ComplianceAuditViewProjection,
) -> str:
    """Route resource URI to appropriate query handler."""
    # Strip query string for routing
    base_uri = uri.split("?")[0]
    query_str = uri[len(base_uri):]
    params: dict[str, str] = {}
    if query_str.startswith("?"):
        qs = parse_qs(query_str[1:])
        params = {k: v[0] for k, v in qs.items()}

    # ledger://ledger/health
    if base_uri == "ledger://ledger/health":
        lags = await daemon.get_all_lags()
        return json.dumps({"projection_lags_ms": lags, "status": "healthy"})

    # Guard: some clients invoke template URIs literally (e.g. {id}).
    if "{id}" in base_uri or "%7Bid%7D" in base_uri:
        return json.dumps({
            "error": "InvalidResourceUri",
            "message": "Replace {id} with a real identifier, e.g. ledger://applications/APEX-0012",
            "uri": base_uri,
        })

    # ledger://applications/{id}
    if base_uri.startswith("ledger://applications/"):
        parts = base_uri[len("ledger://applications/"):].split("/")
        app_id = parts[0]

        # ledger://applications/{id}/compliance
        if len(parts) >= 2 and parts[1] == "compliance":
            as_of = params.get("as_of")
            if as_of:
                ts = datetime.fromisoformat(as_of)
                records = await compliance_proj.get_compliance_at(app_id, ts)
            else:
                records = await compliance_proj.get_current_compliance(app_id)
            return json.dumps(
                {"application_id": app_id, "compliance_records": records},
                default=str,
            )

        # ledger://applications/{id}/audit-trail
        if len(parts) >= 2 and parts[1] == "audit-trail":
            from_ts_str = params.get("from")
            to_ts_str = params.get("to")

            events = await store.load_stream(f"loan-{app_id}")
            result: list[dict[str, Any]] = []
            for e in events:
                if from_ts_str and e.recorded_at < datetime.fromisoformat(from_ts_str):
                    continue
                if to_ts_str and e.recorded_at > datetime.fromisoformat(to_ts_str):
                    continue
                result.append({
                    "event_id": str(e.event_id),
                    "stream_position": e.stream_position,
                    "event_type": e.event_type,
                    "event_version": e.event_version,
                    "payload": e.payload,
                    "recorded_at": e.recorded_at.isoformat(),
                })
            return json.dumps({"application_id": app_id, "events": result})

        # ledger://applications/{id} — current state from projection
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM application_summary WHERE application_id = $1",
                app_id,
            )
        if row is None:
            return json.dumps({"error": "ApplicationNotFound", "application_id": app_id})
        return json.dumps(dict(row), default=str)

    # ledger://agents/{id}/...
    if base_uri.startswith("ledger://agents/"):
        parts = base_uri[len("ledger://agents/"):].split("/")
        agent_id = parts[0]

        # ledger://agents/{id}/sessions/{session_id}
        if len(parts) >= 3 and parts[1] == "sessions":
            session_id = parts[2]
            events = await store.load_stream(f"agent-{agent_id}-{session_id}")
            result = [
                {
                    "event_id": str(e.event_id),
                    "stream_position": e.stream_position,
                    "event_type": e.event_type,
                    "event_version": e.event_version,
                    "payload": e.payload,
                    "recorded_at": e.recorded_at.isoformat(),
                }
                for e in events
            ]
            return json.dumps({
                "agent_id": agent_id,
                "session_id": session_id,
                "events": result,
            })

        # ledger://agents/{id}/performance
        if len(parts) >= 2 and parts[1] == "performance":
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM agent_performance_ledger WHERE agent_id = $1",
                    agent_id,
                )
            return json.dumps(
                {"agent_id": agent_id, "performance": [dict(r) for r in rows]},
                default=str,
            )

    return json.dumps({"error": "UnknownResource", "uri": uri})
