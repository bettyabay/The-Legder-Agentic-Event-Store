"""
Shared async helpers for The Ledger Demo Dashboard.

All backend logic lives here; app.py contains only Streamlit UI layout.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg

from src.db.pool import create_pool, run_migrations
from src.event_store import EventStore
from src.integrity.audit_chain import run_integrity_check
from src.integrity.gas_town import reconstruct_agent_context
from src.mcp.resources import _dispatch_resource
from src.models.events import (
    AgentContextLoaded,
    ApplicationSubmitted,
    CreditAnalysisCompleted,
)
from src.models.exceptions import OptimisticConcurrencyError
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon
from src.what_if.projector import run_what_if

# Wire upcasters once at import time
import src.upcasting.upcasters  # noqa: F401
from src.event_store import set_registry
from src.upcasting.registry import registry

set_registry(registry)

# ─── Constants ─────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SEED_APPS = [
    "APEX-0012", "APEX-0013", "APEX-0014", "APEX-0015",
    "APEX-0016", "APEX-0017", "APEX-0018", "APEX-0019",
    "APEX-0020", "APEX-0021", "APEX-0022", "APEX-0023",
    "APEX-0024", "APEX-0025", "APEX-0026", "APEX-0027",
    "APEX-0028", "APEX-0029",
]

EVENT_SYMBOLS: dict[str, str] = {
    "ApplicationSubmitted": "📋",
    "CreditAnalysisRequested": "🔍",
    "CreditAnalysisCompleted": "📊",
    "FraudScreeningCompleted": "🛡",
    "ComplianceCheckRequested": "📜",
    "ComplianceRulePassed": "✅",
    "ComplianceRuleFailed": "❌",
    "ComplianceClearanceIssued": "🔓",
    "DecisionGenerated": "⚖",
    "HumanReviewCompleted": "👤",
    "ApplicationApproved": "🎉",
    "ApplicationDeclined": "🚫",
    "AgentContextLoaded": "🤖",
    "AuditIntegrityCheckRun": "🔐",
}

# ─── Utilities ─────────────────────────────────────────────────────────────────


def dsn() -> str:
    return os.environ.get(
        "DATABASE_URL", "postgresql://ledger:ledger_dev@localhost:5432/ledger"
    )


def run_async(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def run_migrations_async() -> dict:  # type: ignore[type-arg]
    pool = await create_pool(dsn())
    try:
        await run_migrations(pool)
        return {"ok": True, "message": "Migrations completed."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        await pool.close()


def payload_summary(payload: dict) -> str:  # type: ignore[type-arg]
    if not payload:
        return ""
    keys = (
        "application_id", "applicant_id", "risk_tier",
        "confidence_score", "recommendation", "final_decision",
        "fraud_score", "rule_id",
    )
    parts = [f"{k}={payload[k]}" for k in keys if k in payload and payload[k] is not None]
    if not parts:
        parts = [f"{k}={v}" for k, v in list(payload.items())[:3]]
    return "  ".join(str(p) for p in parts)


def _make_daemon(
    store: EventStore,
    pool: asyncpg.Pool,  # type: ignore[type-arg]
    compliance_proj: ComplianceAuditViewProjection,
) -> ProjectionDaemon:
    return ProjectionDaemon(
        store=store,
        projections=[ApplicationSummaryProjection(), compliance_proj],
        pool=pool,
    )


# ─── Week Standard ─────────────────────────────────────────────────────────────


async def run_week_standard(application_id: str) -> dict:  # type: ignore[type-arg]
    """Full decision history + integrity check. Times the complete query."""
    t0 = time.monotonic()
    pool = await create_pool(dsn())
    store = EventStore(pool=pool)
    compliance_proj = ComplianceAuditViewProjection(pool=pool)
    daemon = _make_daemon(store, pool, compliance_proj)
    try:
        summary_json = await _dispatch_resource(
            f"ledger://applications/{application_id}", store, pool, daemon, compliance_proj
        )
        audit_json = await _dispatch_resource(
            f"ledger://applications/{application_id}/audit-trail", store, pool, daemon, compliance_proj
        )
        compliance_json = await _dispatch_resource(
            f"ledger://applications/{application_id}/compliance", store, pool, daemon, compliance_proj
        )
        health_json = await _dispatch_resource(
            "ledger://ledger/health", store, pool, daemon, compliance_proj
        )
        integrity = await run_integrity_check(store, "loan", application_id)
        elapsed_ms = (time.monotonic() - t0) * 1000
        return {
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "summary": json.loads(summary_json),
            "audit": json.loads(audit_json),
            "compliance": json.loads(compliance_json),
            "health": json.loads(health_json),
            "integrity": {
                "events_verified": integrity.events_verified,
                "chain_valid": integrity.chain_valid,
                "tamper_detected": integrity.tamper_detected,
                "hash": integrity.integrity_hash[:16] + "...",
            },
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "elapsed_ms": (time.monotonic() - t0) * 1000}
    finally:
        await pool.close()


# ─── Concurrency (OCC) Test ────────────────────────────────────────────────────


async def run_concurrency_test() -> dict:  # type: ignore[type-arg]
    """Live double-decision test: two tasks append at expected_version=3."""
    pool = await create_pool(dsn())
    await run_migrations(pool)
    store = EventStore(pool=pool)
    app_id = f"occ-demo-{uuid4().hex[:8]}"
    stream_id = f"loan-{app_id}"
    now = datetime.now(tz=timezone.utc)
    try:
        seed = [
            ApplicationSubmitted(
                application_id=app_id, applicant_id="applicant-occ",
                requested_amount_usd=500_000.0, loan_purpose="OCC demo",
                submission_channel="dashboard", submitted_at=now,
            ),
            CreditAnalysisCompleted(
                application_id=app_id, agent_id="agent-a", session_id="s1",
                model_version="v2.3", confidence_score=0.82, risk_tier="MEDIUM",
                recommended_limit_usd=400_000.0, analysis_duration_ms=1100, input_data_hash="h1",
            ),
            CreditAnalysisCompleted(
                application_id=app_id, agent_id="agent-b", session_id="s2",
                model_version="v2.3", confidence_score=0.77, risk_tier="MEDIUM",
                recommended_limit_usd=380_000.0, analysis_duration_ms=1300, input_data_hash="h2",
            ),
        ]
        await store.append(stream_id, seed, expected_version=-1)

        conflict_event = CreditAnalysisCompleted(
            application_id=app_id, agent_id="agent-fraud", session_id="s-conflict",
            model_version="v2.4", confidence_score=0.65, risk_tier="HIGH",
            recommended_limit_usd=200_000.0, analysis_duration_ms=900, input_data_hash="conflict",
        )
        results: list[Exception | int] = []

        async def attempt() -> None:
            try:
                v = await store.append(stream_id, [conflict_event], expected_version=3)
                results.append(v)
            except OptimisticConcurrencyError as exc:
                results.append(exc)

        await asyncio.gather(attempt(), attempt())

        successes = [r for r in results if isinstance(r, int)]
        failures = [r for r in results if isinstance(r, OptimisticConcurrencyError)]
        events = await store.load_stream(stream_id)
        return {
            "ok": True,
            "app_id": app_id,
            "successes": successes,
            "failures": [
                {"type": "OptimisticConcurrencyError",
                 "expected": f.expected_version, "actual": f.actual_version}
                for f in failures
            ],
            "total_events": len(events),
            "winning_version": successes[0] if successes else None,
            "assertions": {
                "exactly_one_wins": len(successes) == 1,
                "exactly_one_fails": len(failures) == 1,
                "total_events_is_4": len(events) == 4,
                "winning_position_is_4": successes[0] == 4 if successes else False,
            },
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        await pool.close()


# ─── Temporal Compliance Query ─────────────────────────────────────────────────


async def run_temporal_query(application_id: str, as_of: str) -> dict:  # type: ignore[type-arg]
    pool = await create_pool(dsn())
    store = EventStore(pool=pool)
    compliance_proj = ComplianceAuditViewProjection(pool=pool)
    daemon = _make_daemon(store, pool, compliance_proj)
    try:
        current_json = await _dispatch_resource(
            f"ledger://applications/{application_id}/compliance",
            store, pool, daemon, compliance_proj,
        )
        at_time_json = await _dispatch_resource(
            f"ledger://applications/{application_id}/compliance?as_of={as_of}",
            store, pool, daemon, compliance_proj,
        )
        return {
            "ok": True,
            "current": json.loads(current_json),
            "at_time": json.loads(at_time_json),
            "as_of": as_of,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        await pool.close()


# ─── Upcasting Demo ────────────────────────────────────────────────────────────


async def run_upcasting_demo(application_id: str) -> dict:  # type: ignore[type-arg]
    """Show v1 stored payload vs upcasted v2; confirm raw DB row unchanged."""
    pool = await create_pool(dsn())
    store = EventStore(pool=pool)
    try:
        events = await store.load_stream(f"loan-{application_id}")
        credit = next((e for e in events if e.event_type == "CreditAnalysisCompleted"), None)
        if not credit:
            return {"ok": False, "error": "No CreditAnalysisCompleted found in this stream."}
        raw = await pool.fetchrow(
            "SELECT event_version, payload FROM events WHERE event_id = $1",
            credit.event_id,
        )
        raw_payload = json.loads(raw["payload"]) if raw else {}
        return {
            "ok": True,
            "event_id": str(credit.event_id),
            "loaded_version": credit.event_version,
            "loaded_keys": sorted(credit.payload.keys()),
            "raw_db_version": raw["event_version"] if raw else None,
            "raw_db_keys": sorted(raw_payload.keys()),
            "raw_db_payload": raw_payload,
            "upcasted_payload": dict(credit.payload),
            "immutable": raw_payload != dict(credit.payload),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        await pool.close()


# ─── Gas Town Recovery ─────────────────────────────────────────────────────────


async def run_gas_town_demo() -> dict:  # type: ignore[type-arg]
    """Append 5 session events, then reconstruct context without in-memory agent."""
    pool = await create_pool(dsn())
    await run_migrations(pool)
    store = EventStore(pool=pool)
    agent_id = "gas-town-agent"
    session_id = f"gt-{uuid4().hex[:8]}"
    stream_id = f"agent-{agent_id}-{session_id}"
    app_id = f"gt-app-{uuid4().hex[:6]}"
    try:
        batch1 = [
            AgentContextLoaded(
                agent_id=agent_id, session_id=session_id,
                context_source="gas-town-demo", event_replay_from_position=0,
                context_token_count=512, model_version="claude-sonnet-4-20250514",
            ),
            CreditAnalysisCompleted(
                application_id=f"{app_id}-1", agent_id=agent_id, session_id=session_id,
                model_version="claude-sonnet-4-20250514", confidence_score=0.85,
                risk_tier="LOW", recommended_limit_usd=800_000.0,
                analysis_duration_ms=1500, input_data_hash="gt-h1",
            ),
            CreditAnalysisCompleted(
                application_id=f"{app_id}-2", agent_id=agent_id, session_id=session_id,
                model_version="claude-sonnet-4-20250514", confidence_score=0.72,
                risk_tier="MEDIUM", recommended_limit_usd=500_000.0,
                analysis_duration_ms=1800, input_data_hash="gt-h2",
            ),
        ]
        await store.append(stream_id, batch1, expected_version=-1)
        batch2 = [
            CreditAnalysisCompleted(
                application_id=f"{app_id}-3", agent_id=agent_id, session_id=session_id,
                model_version="claude-sonnet-4-20250514", confidence_score=0.61,
                risk_tier="HIGH", recommended_limit_usd=200_000.0,
                analysis_duration_ms=2100, input_data_hash="gt-h3",
            ),
            CreditAnalysisCompleted(
                application_id=f"{app_id}-4", agent_id=agent_id, session_id=session_id,
                model_version="claude-sonnet-4-20250514", confidence_score=0.90,
                risk_tier="LOW", recommended_limit_usd=1_000_000.0,
                analysis_duration_ms=900, input_data_hash="gt-h4",
            ),
        ]
        await store.append(stream_id, batch2, expected_version=3)

        # SIMULATED CRASH — reconstruct with no in-memory agent object
        ctx = await reconstruct_agent_context(store, agent_id, session_id)
        return {
            "ok": True,
            "agent_id": agent_id,
            "session_id": session_id,
            "stream_id": stream_id,
            "events_appended": 5,
            "last_event_position": ctx.last_event_position,
            "session_health": str(ctx.session_health_status),
            "model_version": ctx.model_version,
            "pending_work": ctx.pending_work,
            "context_preview": ctx.context_text[:900],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        await pool.close()


# ─── What-If Analysis ──────────────────────────────────────────────────────────


async def run_what_if_demo(application_id: str, risk_tier: str = "HIGH") -> dict:  # type: ignore[type-arg]
    pool = await create_pool(dsn())
    store = EventStore(pool=pool)
    try:
        events = await store.load_stream(f"loan-{application_id}")
        orig = next((e for e in events if e.event_type == "CreditAnalysisCompleted"), None)
        if not orig:
            return {"ok": False, "error": f"No CreditAnalysisCompleted for {application_id}."}
        op = orig.payload
        cf = CreditAnalysisCompleted(
            application_id=str(op["application_id"]),
            agent_id=str(op["agent_id"]),
            session_id=str(op.get("session_id", "cf-session")),
            model_version=str(op.get("model_version", "cf-model")),
            confidence_score=float(op.get("confidence_score") or 0.75),
            risk_tier=risk_tier,
            recommended_limit_usd=float(op.get("recommended_limit_usd", 500_000))
            * (0.5 if risk_tier == "HIGH" else 1.0),
            analysis_duration_ms=int(op.get("analysis_duration_ms", 1000)),
            input_data_hash=str(op.get("input_data_hash", "cf-hash")),
        )
        t0 = time.monotonic()
        result = await run_what_if(
            store=store,
            application_id=application_id,
            branch_at_event_type="CreditAnalysisCompleted",
            counterfactual_events=[cf],
        )
        return {
            "ok": True,
            "elapsed_ms": (time.monotonic() - t0) * 1000,
            "original_risk_tier": op.get("risk_tier"),
            "counterfactual_risk_tier": risk_tier,
            "real_outcome": result.real_outcome,
            "counterfactual_outcome": result.counterfactual_outcome,
            "divergence_events": result.divergence_events,
            "events_replayed_real": result.events_replayed_real,
            "events_replayed_cf": result.events_replayed_counterfactual,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        await pool.close()


# ─── Agent Performance Metrics ─────────────────────────────────────────────────


async def load_metrics() -> dict:  # type: ignore[type-arg]
    pool = await create_pool(dsn())
    store = EventStore(pool=pool)
    compliance_proj = ComplianceAuditViewProjection(pool=pool)
    daemon = _make_daemon(store, pool, compliance_proj)
    try:
        health_json = await _dispatch_resource(
            "ledger://ledger/health", store, pool, daemon, compliance_proj
        )
        agent_rows = await pool.fetch(
            "SELECT agent_id, model_version, analyses_completed, decisions_generated, "
            "avg_confidence_score, avg_duration_ms, approve_rate, decline_rate, refer_rate, "
            "first_seen_at, last_seen_at FROM agent_performance_ledger ORDER BY last_seen_at DESC LIMIT 50"
        )
        app_states = await pool.fetch(
            "SELECT state, COUNT(*) AS n FROM application_summary GROUP BY state ORDER BY state"
        )
        return {
            "ok": True,
            "health": json.loads(health_json),
            "agent_rows": [dict(r) for r in agent_rows],
            "app_states": [dict(r) for r in app_states],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        await pool.close()


# ─── Subprocess helpers ────────────────────────────────────────────────────────


def run_pipeline(application_id: str, model: str) -> dict:  # type: ignore[type-arg]
    script = PROJECT_ROOT / "scripts" / "run_src_pipeline.py"
    if not script.exists():
        return {"ok": False, "error": f"Script not found: {script}"}
    cmd = [sys.executable, str(script), "--application-id", application_id, "--model", model]
    try:
        result = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0:
            return {"ok": True, "output": (result.stdout or "").strip()}
        return {
            "ok": False,
            "error": result.stderr or result.stdout or f"Exit {result.returncode}",
            "output": result.stdout,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Pipeline timed out (300s)."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def seed_from_jsonl(file_path: str) -> dict:  # type: ignore[type-arg]
    script = PROJECT_ROOT / "scripts" / "seed_from_jsonl.py"
    if not script.exists():
        return {"ok": False, "error": f"Script not found: {script}"}
    cmd = [sys.executable, str(script), "--file", file_path]
    try:
        result = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            return {"ok": True, "message": (result.stdout or "").strip() or f"Seeded from {file_path}."}
        return {"ok": False, "error": result.stderr or result.stdout}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Seed timed out."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
