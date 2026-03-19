"""
Phase 2 — Domain Logic demo dashboard (Streamlit).

Run: uv run streamlit run dashboard/app.py

Requires: PostgreSQL running; run migrations and seed (or use Setup tab) first.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import streamlit as st

# Wire upcasters before any event store / aggregate use
import src.upcasting.upcasters  # noqa: F401
from src.event_store import set_registry
from src.upcasting.registry import registry

set_registry(registry)

from src.aggregates.loan_application import LoanApplicationAggregate
from src.commands.handlers import (
    CreditAnalysisCompletedCommand,
    StartAgentSessionCommand,
    handle_credit_analysis_completed,
    handle_start_agent_session,
)
from src.db.pool import create_pool, run_migrations
from src.event_store import EventStore
from src.models.events import ApplicationState


def _dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://ledger:ledger_dev@localhost:5432/ledger",
    )


def _run_async(coro):
    return asyncio.run(coro)


# Application IDs that have CreditAnalysisRequested in the seed (suitable for Phase 2 demo)
SEED_APPS_AWAITING_ANALYSIS = [
    "APEX-0012", "APEX-0013", "APEX-0014", "APEX-0015", "APEX-0016",
    "APEX-0017", "APEX-0018", "APEX-0019", "APEX-0020", "APEX-0021",
    "APEX-0022", "APEX-0023", "APEX-0024", "APEX-0025", "APEX-0026",
    "APEX-0027", "APEX-0028", "APEX-0029",
]

AGENT_ID = "phase2-dashboard-agent"
SESSION_ID = "phase2-dashboard-session"
MODEL_VERSION = "claude-sonnet-4-20250514"


async def _load_aggregate_state(application_id: str) -> dict:
    pool = await create_pool(_dsn())
    store = EventStore(pool)
    try:
        app = await LoanApplicationAggregate.load(store, application_id)
        return {
            "ok": True,
            "application_id": app.application_id,
            "state": str(app.state) if app.state else "None",
            "version": app.version,
            "applicant_id": getattr(app, "applicant_id", "") or "",
            "requested_amount": getattr(app, "requested_amount", None),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        await pool.close()


async def _run_phase2_demo(application_id: str) -> dict:
    pool = await create_pool(_dsn())
    store = EventStore(pool)
    state_before: str | None = None
    try:
        app = await LoanApplicationAggregate.load(store, application_id)
        state_before = str(app.state) if app.state else "None"
        version_before = app.version

        if app.state != ApplicationState.AWAITING_ANALYSIS:
            return {
                "ok": False,
                "error": f"Application must be in AWAITING_ANALYSIS (got {state_before}). "
                "Choose an application that has CreditAnalysisRequested in the seed.",
                "state_before": state_before,
                "state_after": None,
            }

        await handle_start_agent_session(
            StartAgentSessionCommand(
                agent_id=AGENT_ID,
                session_id=SESSION_ID,
                context_source="phase2-dashboard",
                model_version=MODEL_VERSION,
                context_token_count=1500,
            ),
            store,
        )

        new_version = await handle_credit_analysis_completed(
            CreditAnalysisCompletedCommand(
                application_id=application_id,
                agent_id=AGENT_ID,
                session_id=SESSION_ID,
                model_version=MODEL_VERSION,
                confidence_score=0.82,
                risk_tier="LOW",
                recommended_limit_usd=1_200_000.0,
                duration_ms=2100,
                input_data={"application_id": application_id},
            ),
            store,
        )

        app2 = await LoanApplicationAggregate.load(store, application_id)
        state_after = str(app2.state) if app2.state else "None"

        return {
            "ok": True,
            "state_before": state_before,
            "version_before": version_before,
            "state_after": state_after,
            "version_after": app2.version,
            "new_version": new_version,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "state_before": state_before,
            "state_after": None,
        }
    finally:
        await pool.close()


async def _load_stream_events(stream_id: str) -> dict:
    pool = await create_pool(_dsn())
    store = EventStore(pool)
    try:
        events = await store.load_stream(stream_id)
        rows = []
        for e in events:
            rows.append({
                "Position": e.stream_position,
                "Event type": e.event_type,
                "Recorded at": str(e.recorded_at)[:19] if e.recorded_at else "",
                "Payload (summary)": _payload_summary(e.payload),
            })
        return {"ok": True, "events": rows, "stream_id": stream_id}
    except Exception as e:
        return {"ok": False, "error": str(e), "events": []}
    finally:
        await pool.close()


def _payload_summary(payload: dict) -> str:
    if not payload:
        return ""
    parts = []
    for k in ("application_id", "applicant_id", "state", "risk_tier", "confidence_score", "event_type"):
        if k in payload and payload[k] is not None:
            parts.append(f"{k}={payload[k]}")
    if not parts:
        parts = [f"{k}={v}" for k, v in list(payload.items())[:3]]
    return ", ".join(str(p) for p in parts)


async def _run_migrations() -> dict:
    pool = await create_pool(_dsn())
    try:
        await run_migrations(pool)
        return {"ok": True, "message": "Migrations completed."}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        await pool.close()


def _seed_from_jsonl_sync(file_path: str) -> dict:
    import subprocess
    import sys
    project_root = Path(__file__).resolve().parent.parent
    script = project_root / "scripts" / "seed_from_jsonl.py"
    if not script.exists():
        return {"ok": False, "error": f"Seed script not found: {script}"}
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--file", file_path],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return {"ok": True, "message": (result.stdout or "").strip() or f"Seeded from {file_path}."}
        return {"ok": False, "error": result.stderr or result.stdout or f"Exit code {result.returncode}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Seed timed out."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Phase 2 — Domain Logic Demo", layout="wide")
st.title("Phase 2 — Domain Logic Demo")
st.caption("Aggregates, command handlers & business rules with seeded event store")

tab1, tab2, tab3 = st.tabs(["Run Demo", "Event stream", "Setup"])

with tab1:
    st.header("Run Demo")
    app_id = st.selectbox(
        "Application ID (seed apps with CreditAnalysisRequested)",
        options=SEED_APPS_AWAITING_ANALYSIS,
        index=0,
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Load aggregate (view state)", key="load_agg"):
            with st.spinner("Loading..."):
                out = _run_async(_load_aggregate_state(app_id))
            if out.get("ok"):
                st.success("Aggregate loaded")
                st.json({
                    "application_id": out["application_id"],
                    "state": out["state"],
                    "version": out["version"],
                    "applicant_id": out.get("applicant_id"),
                    "requested_amount": out.get("requested_amount"),
                })
            else:
                st.error(out.get("error", "Unknown error"))

    with col2:
        if st.button("Run full Phase 2 demo", key="run_demo"):
            with st.spinner("Running: load → start session → credit analysis → re-load..."):
                out = _run_async(_run_phase2_demo(app_id))
            if out.get("ok"):
                st.success("Phase 2 demo completed successfully.")
                st.json({
                    "State before": out["state_before"],
                    "Version before": out["version_before"],
                    "State after": out["state_after"],
                    "Version after": out["version_after"],
                })
            else:
                st.error(out.get("error", "Unknown error"))
                if out.get("state_before") is not None:
                    st.info(f"State before: {out['state_before']}")

    st.divider()
    st.markdown("""
    **Steps (when you click "Run full Phase 2 demo"):**
    1. Reconstruct `LoanApplication` from event history → must be **AWAITING_ANALYSIS**.
    2. Start agent session → append **AgentContextLoaded** (Gas Town).
    3. Append **CreditAnalysisCompleted** to loan stream (BR-1, BR-2, BR-3).
    4. Re-load aggregate → state **ANALYSIS_COMPLETE**.
    """)

with tab2:
    st.header("Event stream")
    stream_id = st.text_input("Stream ID", value="loan-APEX-0012", key="stream_id")
    if st.button("Load events", key="load_events"):
        with st.spinner("Loading stream..."):
            out = _run_async(_load_stream_events(stream_id))
        if out.get("ok"):
            st.success(f"Loaded {len(out['events'])} events from `{out['stream_id']}`")
            if out["events"]:
                st.dataframe(out["events"], use_container_width=True)
            else:
                st.info("No events in this stream.")
        else:
            st.error(out.get("error", "Unknown error"))

with tab3:
    st.header("Setup")
    st.caption("Run migrations and/or seed from JSONL (e.g. after a fresh DB).")
    if st.button("Run migrations", key="migrate"):
        with st.spinner("Running migrations..."):
            out = _run_async(_run_migrations())
        if out.get("ok"):
            st.success(out.get("message"))
        else:
            st.error(out.get("error"))

    default_seed = str(Path(__file__).resolve().parent.parent / "starter" / "data" / "seed_events.jsonl")
    seed_path = st.text_input("Seed JSONL path", value=default_seed, key="seed_path")
    if st.button("Seed from JSONL", key="seed"):
        with st.spinner("Seeding..."):
            out = _seed_from_jsonl_sync(seed_path)
        if out.get("ok"):
            st.success(out.get("message"))
        else:
            st.error(out.get("error"))
