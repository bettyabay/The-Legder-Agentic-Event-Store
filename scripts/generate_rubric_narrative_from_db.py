from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

from src.db.pool import create_pool
from src.event_store import EventStore
from src.integrity.audit_chain import run_integrity_check
from src.integrity.gas_town import reconstruct_agent_context
from src.llm.client import create_async_anthropic_client
from src.mcp.resources import _dispatch_resource
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon

# Wire upcasters so event payloads have latest fields.
import src.upcasting.upcasters  # noqa: F401
from src.event_store import set_registry
from src.upcasting.registry import registry

set_registry(registry)


def _safe_json_dumps(obj: Any, *, max_chars: int = 12_000) -> str:
    text = json.dumps(obj, default=str, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]..."


def _summarise_events(events: list[dict[str, Any]], *, max_items: int = 40) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in events[-max_items:]:
        payload = e.get("payload") or {}
        payload_preview: str | None = None
        if isinstance(payload, dict):
            payload_preview = _safe_json_dumps(
                {k: payload.get(k) for k in list(payload.keys())[:12]},
                max_chars=2000,
            )
        else:
            payload_preview = _safe_json_dumps(payload, max_chars=2000)
        out.append(
            {
                "stream_position": e.get("stream_position"),
                "event_type": e.get("event_type"),
                "event_version": e.get("event_version"),
                "recorded_at": str(e.get("recorded_at"))[:19],
                "payload_preview": payload_preview,
            }
        )
    return out


async def _load_mcp_resource_json(
    uri: str,
    *,
    store: EventStore,
    pool: asyncpg.Pool,
    daemon: ProjectionDaemon,
    compliance_proj: ComplianceAuditViewProjection,
) -> dict[str, Any]:
    raw = await _dispatch_resource(uri, store, pool, daemon, compliance_proj)
    try:
        return json.loads(raw)
    except Exception:
        return {"error": "Resource returned non-JSON", "raw": raw}


async def _find_gas_town_failed_session(
    pool: asyncpg.Pool,
    *,
    application_id: str,
) -> dict[str, str] | None:
    # Look for AgentSessionFailed / AgentSessionRecovered rows that belong to this application.
    # We rely on JSONB payload fields existing for these events.
    rows = await pool.fetch(
        """
        SELECT event_type,
               payload->>'agent_id' AS agent_id,
               payload->>'session_id' AS session_id,
               payload->>'last_successful_node' AS last_successful_node,
               recorded_at
        FROM events
        WHERE payload->>'application_id' = $1
          AND event_type IN ('AgentSessionFailed','AgentSessionRecovered','AgentContextLoaded')
        ORDER BY recorded_at DESC
        LIMIT 1
        """,
        application_id,
    )
    if not rows:
        return None
    r = rows[0]
    if not r.get("session_id") or not r.get("agent_id"):
        return None
    return {
        "event_type": str(r.get("event_type") or ""),
        "agent_id": str(r.get("agent_id")),
        "session_id": str(r.get("session_id")),
        "last_successful_node": str(r.get("last_successful_node") or ""),
        "recorded_at": str(r.get("recorded_at") or "")[:19],
    }


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="Generate LLM rubric narrative grounded in stored DB events.")
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--model", default="claude-sonnet-4-20250514")
    parser.add_argument("--out", default="artifacts/narrative_rubric_llm.txt")
    parser.add_argument("--token-budget", type=int, default=8000)
    parser.add_argument("--max-events", type=int, default=60)
    args = parser.parse_args()

    application_id = args.application_id.strip()
    # Script expects canonical IDs; keep as-is if already canonical.
    if application_id.isdigit():
        application_id = f"APEX-{application_id}"

    pool = await create_pool()
    try:
        store = EventStore(pool=pool)
        compliance_proj = ComplianceAuditViewProjection(pool=pool)
        daemon = ProjectionDaemon(store=store, projections=[], pool=pool)  # type: ignore[arg-type]

        # For resources dispatch we still need a daemon with projections; minimal setup:
        daemon = ProjectionDaemon(
            store=store,
            projections=[compliance_proj],
            pool=pool,
        )

        # Primary evidence: audit trail + compliance.
        audit = await _load_mcp_resource_json(
            f"ledger://applications/{application_id}/audit-trail",
            store=store,
            pool=pool,
            daemon=daemon,
            compliance_proj=compliance_proj,
        )
        compliance = await _load_mcp_resource_json(
            f"ledger://applications/{application_id}/compliance",
            store=store,
            pool=pool,
            daemon=daemon,
            compliance_proj=compliance_proj,
        )

        loan_events: list[dict[str, Any]] = audit.get("events") or []

        # Integrity check (cryptographic audit chain) on the loan primary stream.
        integrity = await run_integrity_check(store, "loan", application_id)

        # Gas Town evidence: find a failed/recovered session and reconstruct.
        gas_session = await _find_gas_town_failed_session(pool, application_id=application_id)
        gas_town: dict[str, Any] = {"found": gas_session is not None}
        if gas_session:
            ctx = await reconstruct_agent_context(
                store=store,
                agent_id=gas_session["agent_id"],
                session_id=gas_session["session_id"],
                token_budget=args.token_budget,
            )
            gas_town.update(
                {
                    "agent_id": ctx.agent_id,
                    "session_id": ctx.session_id,
                    "session_health_status": str(ctx.session_health_status),
                    "last_successful_node": ctx.last_successful_node,
                    "executed_nodes": ctx.executed_nodes[-30:],
                    "pending_work": ctx.pending_work[:20],
                    "context_preview": ctx.context_text[:1200],
                }
            )

        # Build compact evidence input for the LLM.
        evidence = {
            "application_id": application_id,
            "evidence_generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "integrity": {
                "events_verified": integrity.events_verified,
                "chain_valid": integrity.chain_valid,
                "tamper_detected": integrity.tamper_detected,
                "integrity_hash_prefix": integrity.integrity_hash[:16],
            },
            "gas_town": gas_town,
            "loan_timeline": _summarise_events(loan_events, max_items=args.max_events),
            "compliance_records": (compliance.get("compliance_records") or [])[:120],
        }

        system = (
            "You are generating the Week-5 rubric narrative evidence for an agentic event store project. "
            "You must NOT invent events. Use only the provided evidence JSON. "
            "Output markdown with 5 sections: NARR-01 OCC/Collision, NARR-02 Doc extraction failure, "
            "NARR-03 Agent crash recovery (Gas Town), NARR-04 Compliance hard block, NARR-05 Human override. "
            "For each section: (1) state whether the scenario is observed in the evidence; (2) cite the "
            "specific event_types and recorded_at timestamps; (3) give a short rubric-style narrative; "
            "(4) if not observed, you MUST ONLY write: "
            "'Not observed in stored events' and an 'Evidence Required:' line listing the "
            "event_types that would be needed. Do not add any speculative narrative."
        )
        user = (
            f"Application: {application_id}\n\n"
            f"Evidence JSON (may be truncated):\n{_safe_json_dumps(evidence, max_chars=25_000)}"
        )

        client = create_async_anthropic_client()
        resp = await client.messages.create(
            model=args.model,
            max_tokens=1100,
            system=system,
            messages=[{"role": "user", "content": user}],
        )

        text = resp.content[0].text

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"OK: wrote {out_path}")
    finally:
        await pool.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main_async())

