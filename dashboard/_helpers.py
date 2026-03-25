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
        # CreditAnalysisCompleted events belong to the credit stream (credit-{application_id}),
        # not the loan stream (loan-{application_id}).
        events = await store.load_stream(f"credit-{application_id}")
        event_types = sorted({e.event_type for e in events})
        credit = next((e for e in events if e.event_type == "CreditAnalysisCompleted"), None)
        if not credit:
            return {
                "ok": False,
                "error": "No CreditAnalysisCompleted found in this credit stream.",
                "credit_stream_id": f"credit-{application_id}",
                "credit_stream_count": len(events),
                "credit_stream_types": event_types[:50],
                "hint": (
                    "Your database appears not to have processed credit analysis for this application yet. "
                    "Run the full agent pipeline in dashboard Tab 8 (for this app id) to generate "
                    "`CreditAnalysisCompleted` events in `credit-{application_id}`, then retry."
                ),
            }
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


async def run_gas_town_replay_for_app(application_id: str, *, token_budget: int = 8000) -> dict:
    """Replay a real stored agent session for a given application_id.

    This does NOT append new events. It demonstrates that Gas Town reconstruction
    can rebuild agent context from persisted session streams.
    """
    pool = await create_pool(dsn())
    store = EventStore(pool=pool)
    await run_migrations(pool)
    try:
        # Pick the most recent AgentSessionStarted for this application.
        row = await pool.fetchrow(
            """
            SELECT
              payload->>'session_id' AS session_id,
              payload->>'agent_type' AS agent_type,
              payload->>'agent_id' AS agent_id,
              payload->>'model_version' AS model_version,
              recorded_at
            FROM events
            WHERE event_type = 'AgentSessionStarted'
              AND payload->>'application_id' = $1
              AND payload->>'session_id' IS NOT NULL
            ORDER BY recorded_at DESC
            LIMIT 1
            """,
            application_id,
        )
        if not row or not row.get("session_id"):
            return {
                "ok": False,
                "error": "No AgentSessionStarted found for this application_id.",
                "application_id": application_id,
            }

        session_id = str(row["session_id"])
        agent_type = str(row.get("agent_type") or "")
        # For week-5 canonical session streams, the stream prefix is agent_type.value.
        # Gas Town reconstruction expects that in its first stream lookup.
        agent_id_for_replay = agent_type or str(row.get("agent_id") or "")

        ctx = await reconstruct_agent_context(
            store=store,
            agent_id=agent_id_for_replay,
            session_id=session_id,
            token_budget=token_budget,
        )
        return {
            "ok": True,
            "application_id": application_id,
            "session_id": ctx.session_id,
            "agent_id": ctx.agent_id,
            "model_version": ctx.model_version or str(row.get("model_version") or ""),
            "last_event_position": ctx.last_event_position,
            "session_health": str(ctx.session_health_status),
            "pending_work": ctx.pending_work,
            "executed_nodes": ctx.executed_nodes,
            "context_preview": ctx.context_text[:900],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    finally:
        await pool.close()


# ─── What-If Analysis ──────────────────────────────────────────────────────────


async def run_what_if_demo(application_id: str, risk_tier: str = "HIGH") -> dict:  # type: ignore[type-arg]
    pool = await create_pool(dsn())
    store = EventStore(pool=pool)
    try:
        loan_stream_id = f"loan-{application_id}"
        credit_stream_id = f"credit-{application_id}"

        # CreditAnalysisRequested/Completed are split across streams:
        # - projector replays the loan stream (loan-{application_id})
        # - the pipeline emits CreditAnalysisCompleted into credit-{application_id}
        loan_events = await store.load_stream(loan_stream_id)
        credit_events = await store.load_stream(credit_stream_id)

        # Prefer the credit stream for the "original" credit analysis outcome.
        credit_completed = next(
            (e for e in reversed(credit_events) if e.event_type == "CreditAnalysisCompleted"),
            None,
        )
        loan_completed = next(
            (e for e in reversed(loan_events) if e.event_type == "CreditAnalysisCompleted"),
            None,
        )

        orig = credit_completed or loan_completed
        if not orig:
            credit_completed_count = sum(
                1 for e in credit_events if e.event_type == "CreditAnalysisCompleted"
            )
            loan_completed_count = sum(
                1 for e in loan_events if e.event_type == "CreditAnalysisCompleted"
            )
            loan_requested_count = sum(
                1 for e in loan_events if e.event_type == "CreditAnalysisRequested"
            )
            return {
                "ok": False,
                "error": (
                    "No CreditAnalysisCompleted available for counterfactual branching. "
                    f"For {application_id}: "
                    f"loan_stream completed={loan_completed_count}, credit_stream completed={credit_completed_count}, "
                    f"loan_stream requested={loan_requested_count}. "
                    "Run the full agent pipeline in dashboard Tab 8 to generate the missing "
                    f"CreditAnalysisCompleted event(s) (expected in `{credit_stream_id}`), then retry."
                ),
            }

        op = orig.payload
        decision = op.get("decision") if isinstance(op, dict) else None
        if isinstance(decision, dict):
            orig_risk_tier = decision.get("risk_tier")
            orig_confidence = decision.get("confidence")
            orig_recommended_limit = decision.get("recommended_limit_usd")
        else:
            # Fallback for any legacy payloads that store these fields flat.
            orig_risk_tier = op.get("risk_tier") if isinstance(op, dict) else None
            orig_confidence = op.get("confidence_score") if isinstance(op, dict) else None
            orig_recommended_limit = op.get("recommended_limit_usd") if isinstance(op, dict) else None

        if orig_risk_tier is None or orig_recommended_limit is None:
            return {"ok": False, "error": f"Credit payload missing decision fields for {application_id}."}

        # DB payload uses numeric fields as strings in some places; normalize to float.
        try:
            orig_recommended_limit_f = float(orig_recommended_limit)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"Invalid recommended_limit_usd in credit payload for {application_id}."}

        # The stored canonical payload in this repo does not include agent_id.
        agent_id = op.get("agent_id") if isinstance(op, dict) else None
        if not isinstance(agent_id, str) or not agent_id:
            agent_id = "credit_agent"

        regulatory_basis = op.get("regulatory_basis") if isinstance(op, dict) else None
        # In stored payloads this is often [] (empty list). The event model expects str|None.
        if not isinstance(regulatory_basis, str):
            regulatory_basis = None

        cf = CreditAnalysisCompleted(
            application_id=str(op.get("application_id") or application_id),
            agent_id=agent_id,
            session_id=str(op.get("session_id") or "cf-session"),
            model_version=str(op.get("model_version") or "cf-model"),
            confidence_score=float(orig_confidence) if orig_confidence is not None else None,
            risk_tier=risk_tier,
            recommended_limit_usd=orig_recommended_limit_f
            * (0.5 if risk_tier == "HIGH" else 1.0),
            analysis_duration_ms=int(op.get("analysis_duration_ms") or 1000),
            input_data_hash=str(op.get("input_data_hash") or "cf-hash"),
            regulatory_basis=regulatory_basis,
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
            "original_risk_tier": orig_risk_tier,
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
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
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


def normalize_application_id(application_id: str) -> str:
    """Normalize user-friendly app ids like '0012' → 'APEX-0012'."""
    raw = (application_id or "").strip()
    if not raw:
        return raw
    if raw.startswith("APEX-"):
        return raw
    # Week-5 seeded app IDs use 'APEX-####' format.
    if raw.isdigit():
        return f"APEX-{raw}"
    return raw


def _read_text_artifact(path: Path, *, max_chars: int = 50_000) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"ok": "false", "error": str(exc)}

    if len(text) <= max_chars:
        return {"ok": "true", "content": text}
    return {"ok": "true", "content": text[:max_chars] + "\n...[truncated]..."}


def _read_json_artifact(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
        return {"ok": True, "json": json.loads(text)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def run_pytest_capture(test_path: str, *, timeout_s: int = 600) -> dict:  # type: ignore[type-arg]
    """Run pytest for a single test file and return combined output."""
    cmd = [sys.executable, "-m", "pytest", test_path, "-v"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "output": output.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "error": f"pytest timed out after {timeout_s}s"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exit_code": None, "error": str(exc)}


def generate_week5_assessment_artifacts(application_id: str, *, out_dir: str = "artifacts") -> dict:  # type: ignore[type-arg]
    """Generate rubric evidence artifacts (narrative, cost, regulatory package)."""
    script = PROJECT_ROOT / "scripts" / "generate_week5_artifacts.py"
    if not script.exists():
        return {"ok": False, "error": f"Script not found: {script}"}

    norm_app_id = normalize_application_id(application_id)
    cmd = [
        sys.executable,
        str(script),
        "--application-id",
        norm_app_id,
        "--out-dir",
        out_dir,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "application_id": norm_app_id,
            "output": output.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "error": "Artifact generation timed out (900s)."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exit_code": None, "error": str(exc)}


def load_week5_artifacts(application_id: str, *, out_dir: str = "artifacts") -> dict:  # type: ignore[type-arg]
    """Load generated artifact contents for UI rendering."""
    norm_app_id = normalize_application_id(application_id)
    artifacts_dir = PROJECT_ROOT / out_dir
    if not artifacts_dir.exists():
        return {"ok": False, "error": f"Artifacts folder not found: {artifacts_dir}"}

    narrative_path = artifacts_dir / "narrative_test_results.txt"
    rubric_llm_path = artifacts_dir / f"narrative_rubric_llm_{norm_app_id}.txt"
    cost_path = artifacts_dir / "api_cost_report.txt"
    log_path = artifacts_dir / "artifact_generation_log.txt"
    regulatory_path = artifacts_dir / f"regulatory_package_{norm_app_id}.json"

    narrative = _read_text_artifact(narrative_path)
    cost = _read_text_artifact(cost_path)
    log = _read_text_artifact(log_path)
    regulatory = _read_json_artifact(regulatory_path)

    rubric_llm = _read_text_artifact(rubric_llm_path) if rubric_llm_path.exists() else {"ok": "false", "error": "Missing rubric llm file"}

    return {
        "ok": True,
        "application_id": norm_app_id,
        "paths": {
            "narrative_test_results": str(narrative_path),
            "narrative_rubric_llm": str(rubric_llm_path),
            "api_cost_report": str(cost_path),
            "artifact_generation_log": str(log_path),
            "regulatory_package": str(regulatory_path),
        },
        "narrative_test_results": narrative.get("content", "") if narrative.get("ok") == "true" else None,
        "api_cost_report": cost.get("content", "") if cost.get("ok") == "true" else None,
        "artifact_generation_log": log.get("content", "") if log.get("ok") == "true" else None,
        "regulatory_package": regulatory.get("json") if regulatory.get("ok") else None,
        "regulatory_package_error": regulatory.get("error") if not regulatory.get("ok") else None,
        "rubric_narrative_text": rubric_llm.get("content", "") if rubric_llm.get("ok") == "true" else None,
    }


def generate_regulatory_package_artifact(application_id: str, *, out_dir: str = "artifacts") -> dict:  # type: ignore[type-arg]
    """Generate only the regulatory package JSON for one application."""
    script = PROJECT_ROOT / "scripts" / "generate_regulatory_package_artifact.py"
    if not script.exists():
        return {"ok": False, "error": f"Script not found: {script}"}

    norm_app_id = normalize_application_id(application_id)
    out_path = str(Path(out_dir) / f"regulatory_package_{norm_app_id}.json")

    cmd = [
        sys.executable,
        str(script),
        "--application-id",
        norm_app_id,
        "--out",
        out_path,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "output": output.strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "error": "Regulatory package generation timed out (300s)."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exit_code": None, "error": str(exc)}


def load_week5_global_artifacts(*, out_dir: str = "artifacts") -> dict:  # type: ignore[type-arg]
    """Load narrative + cost artifacts that are not application-specific."""
    artifacts_dir = PROJECT_ROOT / out_dir
    narrative_path = artifacts_dir / "narrative_test_results.txt"
    cost_path = artifacts_dir / "api_cost_report.txt"
    log_path = artifacts_dir / "artifact_generation_log.txt"

    narrative = _read_text_artifact(narrative_path)
    cost = _read_text_artifact(cost_path)
    log = _read_text_artifact(log_path)

    return {
        "ok": narrative.get("ok") == "true" and cost.get("ok") == "true" and log.get("ok") == "true",
        "narrative_test_results": narrative.get("content") if narrative.get("ok") == "true" else None,
        "api_cost_report": cost.get("content") if cost.get("ok") == "true" else None,
        "artifact_generation_log": log.get("content") if log.get("ok") == "true" else None,
        "errors": {
            "narrative": narrative.get("error") if narrative.get("ok") != "true" else None,
            "cost": cost.get("error") if cost.get("ok") != "true" else None,
            "log": log.get("error") if log.get("ok") != "true" else None,
        },
        "paths": {
            "narrative_test_results": str(narrative_path),
            "api_cost_report": str(cost_path),
            "artifact_generation_log": str(log_path),
        },
    }


def generate_api_cost_report_artifact(*, out_dir: str = "artifacts") -> dict:
    """Generate api_cost_report.txt (DB-derived) for UI display."""
    script = PROJECT_ROOT / "scripts" / "generate_api_cost_report.py"
    if not script.exists():
        return {"ok": False, "error": f"Script not found: {script}"}

    out_path = str(Path(out_dir) / "api_cost_report.txt")
    cmd = [
        sys.executable,
        str(script),
        "--out",
        out_path,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "output": output.strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "error": "api cost report generation timed out (300s)."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exit_code": None, "error": str(exc)}


def generate_query_history_artifact(application_id: str, *, out_dir: str = "artifacts") -> dict:
    """Generate a DB-derived event timeline by calling scripts/query_history.py."""
    script = PROJECT_ROOT / "scripts" / "query_history.py"
    if not script.exists():
        return {"ok": False, "error": f"Script not found: {script}"}

    norm_app_id = normalize_application_id(application_id)
    out_path = Path(out_dir) / f"real_event_timeline_{norm_app_id}.txt"

    cmd = [
        sys.executable,
        str(script),
        "--application-id",
        norm_app_id,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "output_path": str(out_path),
            "output": output.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "error": "query_history timed out (180s)."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exit_code": None, "error": str(exc)}


def generate_real_llm_evidence(
    application_id: str,
    model: str,
    *,
    out_dir: str = "artifacts",
) -> dict:  # type: ignore[type-arg]
    """Run real agents (LLM) and generate DB-derived evidence artifacts."""
    norm_app_id = normalize_application_id(application_id)

    # 1. Run agents with real LLM calls.
    pipe = run_pipeline(norm_app_id, model)
    if not pipe.get("ok"):
        return {"ok": False, "error": pipe.get("error") or "Pipeline failed.", "pipeline": pipe}

    # 2. DB-derived narrative timeline (no fake/test output).
    timeline = generate_query_history_artifact(norm_app_id, out_dir=out_dir)
    if not timeline.get("ok"):
        return {
            "ok": False,
            "error": timeline.get("error") or "Timeline generation failed.",
            "pipeline": pipe,
            "timeline": timeline,
        }

    # 3. DB-derived cost report.
    cost = generate_api_cost_report_artifact(out_dir=out_dir)
    if not cost.get("ok"):
        return {
            "ok": False,
            "error": cost.get("error") or "Cost report failed.",
            "pipeline": pipe,
            "timeline": timeline,
            "cost": cost,
        }

    api_cost_report_path = Path(out_dir) / "api_cost_report.txt"
    api_cost_report_text = ""
    if api_cost_report_path.exists():
        api_cost_report_text = api_cost_report_path.read_text(encoding="utf-8")

    # 4. DB-derived regulatory package.
    reg = generate_regulatory_package_artifact(norm_app_id, out_dir=out_dir)
    if not reg.get("ok"):
        return {
            "ok": False,
            "error": reg.get("error") or "Regulatory package generation failed.",
            "pipeline": pipe,
            "timeline": timeline,
            "regulatory": reg,
        }

    regulatory_path = Path(out_dir) / f"regulatory_package_{norm_app_id}.json"
    regulatory_json: dict[str, object] | None = None
    regulatory_read = _read_json_artifact(regulatory_path)
    if regulatory_read.get("ok") is True:
        regulatory_json = regulatory_read.get("json")  # type: ignore[assignment]

    # 5. LLM-written rubric narrative grounded in stored DB evidence.
    rubric = generate_rubric_narrative_llm_artifact(norm_app_id, model, out_dir=out_dir)
    rubric_text = rubric.get("rubric_narrative_text") if rubric.get("ok") else None

    return {
        "ok": True,
        "application_id": norm_app_id,
        "pipeline": pipe,
        "timeline": timeline,
        "cost": cost,
        "api_cost_report_text": api_cost_report_text,
        "regulatory": reg,
        "regulatory_package_json": regulatory_json,
        "rubric_narrative_text": rubric_text,
        "rubric_narrative": rubric,
    }


def generate_real_db_evidence(
    application_id: str,
    *,
    model: str,
    out_dir: str = "artifacts",
) -> dict:  # type: ignore[type-arg]
    """Generate evidence from already-stored DB events (and write rubric via LLM)."""
    norm_app_id = normalize_application_id(application_id)

    timeline = generate_query_history_artifact(norm_app_id, out_dir=out_dir)
    if not timeline.get("ok"):
        return {"ok": False, "error": timeline.get("error") or "Timeline generation failed.", "timeline": timeline}

    cost = generate_api_cost_report_artifact(out_dir=out_dir)
    api_cost_report_text = ""
    if Path(out_dir, "api_cost_report.txt").exists():
        api_cost_report_text = Path(out_dir, "api_cost_report.txt").read_text(encoding="utf-8")

    # Cost report is global (all apps) since scripts/generate_api_cost_report.py is DB-wide.
    # That's still useful as a rubric artifact, but if you need per-app cost, we should add a new script.

    reg = generate_regulatory_package_artifact(norm_app_id, out_dir=out_dir)
    if not reg.get("ok"):
        return {"ok": False, "error": reg.get("error") or "Regulatory package generation failed.", "timeline": timeline, "cost": cost, "regulatory": reg}

    regulatory_path = Path(out_dir) / f"regulatory_package_{norm_app_id}.json"
    regulatory_json: dict[str, object] | None = None
    regulatory_read = _read_json_artifact(regulatory_path)
    if regulatory_read.get("ok") is True:
        regulatory_json = regulatory_read.get("json")  # type: ignore[assignment]

    rubric = generate_rubric_narrative_llm_artifact(norm_app_id, model, out_dir=out_dir)
    rubric_text = rubric.get("rubric_narrative_text") if rubric.get("ok") else None

    return {
        "ok": True,
        "application_id": norm_app_id,
        "pipeline": None,
        "timeline": timeline,
        "cost": cost,
        "api_cost_report_text": api_cost_report_text,
        "regulatory": reg,
        "regulatory_package_json": regulatory_json,
        "rubric_narrative_text": rubric_text,
        "rubric_narrative": rubric,
    }


def generate_rubric_narrative_llm_artifact(
    application_id: str,
    model: str,
    *,
    out_dir: str = "artifacts",
) -> dict:  # type: ignore[type-arg]
    """Generate LLM-written rubric narrative grounded in stored DB events."""
    script = PROJECT_ROOT / "scripts" / "generate_rubric_narrative_from_db.py"
    if not script.exists():
        return {"ok": False, "error": f"Script not found: {script}"}

    norm_app_id = normalize_application_id(application_id)
    out_path = Path(out_dir) / f"narrative_rubric_llm_{norm_app_id}.txt"

    cmd = [
        sys.executable,
        str(script),
        "--application-id",
        norm_app_id,
        "--model",
        model,
        "--out",
        str(out_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        content_read = _read_text_artifact(out_path)
        if proc.returncode != 0:
            return {"ok": False, "exit_code": proc.returncode, "error": content_read.get("error") or output.strip()}
        return {
            "ok": True,
            "exit_code": proc.returncode,
            "output_path": str(out_path),
            "rubric_narrative_text": content_read.get("content", ""),
            "raw_output": output.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "error": "Rubric narrative generation timed out (900s)."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exit_code": None, "error": str(exc)}
