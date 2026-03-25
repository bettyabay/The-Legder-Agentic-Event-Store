"""
The Ledger — Demo Dashboard

Full-lifecycle presentation of the Agentic Event Store (TRP1 Week 5).
Covers all 6 video-demo steps + agent metrics + live pipeline runner + setup.

Run:
    uv run streamlit run dashboard/app.py
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from dashboard._helpers import (
    EVENT_SYMBOLS,
    SEED_APPS,
    load_metrics,
    payload_summary,
    # theme
    run_async,
    run_concurrency_test,
    run_gas_town_demo,
    run_gas_town_replay_for_app,
    run_migrations_async,
    generate_week5_assessment_artifacts,
    generate_regulatory_package_artifact,
    generate_real_llm_evidence,
    generate_real_db_evidence,
    load_week5_artifacts,
    load_week5_global_artifacts,
    normalize_application_id,
    run_pytest_capture,
    run_pipeline,
    run_temporal_query,
    run_upcasting_demo,
    run_week_standard,
    run_what_if_demo,
    seed_from_jsonl,
)

# ─── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="The Ledger — Demo Dashboard",
    page_icon="📒",
    layout="wide",
)

# ─── Theme injection & Hero header ─────────────────────────────────────────────

_CSS = (Path(__file__).resolve().parent / "theme.css").read_text(encoding="utf-8")
st.markdown(f"<style>{_CSS}</style>", unsafe_allow_html=True)

col_a, col_b = st.columns([6, 1])
with col_a:
    st.markdown(
        """
        <div class="ledger-hero" id="ledger-hero">
          <div class="brand">
            <div class="logo">⚡</div>
            <div>
              <div style="font-size:18px;line-height:1;">The Ledger — Agentic Event Store</div>
              <div class="sub" style="font-size:12px;">TRP1 Week 5 · Apex Financial Services</div>
            </div>
          </div>
          <div class="badge">CQRS · OCC · Upcasting · Hash Chain</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
with col_b:
    dark = st.toggle("Dark mode", value=True, key="theme_dark")

# Streamlit can sanitize/ignore inline JS in markdown; use CSS override on rerun.
if dark:
    theme_override = """
    :root {
      --bg: #0e1117;
      --surface: #141a23;
      --surface-2: #1b2330;
      --text: #e6edf3;
      --muted: #9aa4b2;
      --shadow: 0 8px 28px rgba(2, 6, 23, 0.35);
      --ring: 0 0 0 1px rgba(45, 212, 191, 0.35);
    }
    """
else:
    theme_override = """
    :root {
      --bg: #f7fafc;
      --surface: #ffffff;
      --surface-2: #f2f6fa;
      --text: #0f172a;
      --muted: #64748b;
      --shadow: 0 8px 28px rgba(2, 6, 23, 0.08);
      --ring: 0 0 0 1px rgba(20, 184, 166, 0.25);
    }
    """
st.markdown(f"<style>{theme_override}</style>", unsafe_allow_html=True)

# ─── Tabs ──────────────────────────────────────────────────────────────────────

(
    tab_week, tab_occ, tab_temporal,
    tab_upcast, tab_gas, tab_whatif,
    tab_metrics, tab_run, tab_setup,
    tab_evidence,
) = st.tabs([
    "🏆 Week Standard",
    "⚡ Concurrency",
    "🕐 Temporal Query",
    "🔄 Upcasting",
    "🤖 Gas Town",
    "🔀 What-If",
    "📊 Metrics",
    "🚀 Run Agents",
    "⚙️ Setup",
    "🧾 Assessment Evidence Pack",
])

# ─── Tab 1 · Week Standard ─────────────────────────────────────────────────────

with tab_week:
    st.header("Step 1 — Complete Decision History (Week Standard)")
    st.caption("Show full event stream, agent actions, compliance checks, causal links, and integrity. Must complete in < 60s.")
    app_id = st.selectbox("Application ID", SEED_APPS, key="ws_app")
    if st.button("▶ Run (timed)", key="ws_run"):
        with st.spinner("Querying full decision history…"):
            out = run_async(run_week_standard(app_id))
        elapsed = out.get("elapsed_ms", 0)
        badge = "✅ PASS (<60s)" if elapsed < 60_000 else "❌ OVER 60s"
        st.metric("Total query time", f"{elapsed:.0f} ms", delta=badge)

        if out.get("ok"):
            summary = out.get("summary", {})
            amount_raw = summary.get("requested_amount_usd", 0)
            try:
                amount_text = f"${float(amount_raw):,.0f}"
            except (TypeError, ValueError):
                amount_text = str(amount_raw) if amount_raw is not None else "$0"
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("State", summary.get("state", "?"))
            c2.metric("Risk Tier", summary.get("risk_tier") or "—")
            c3.metric("Decision", summary.get("decision") or "—")
            c4.metric("Amount", amount_text)

            st.subheader("📜 Audit Trail")
            rows = [
                {
                    "Pos": e.get("stream_position", "?"),
                    "": EVENT_SYMBOLS.get(e["event_type"], "·"),
                    "Event Type": e["event_type"],
                    "Recorded At": str(e.get("recorded_at", ""))[:19],
                    "Details": payload_summary(e.get("payload", {})),
                }
                for e in out.get("audit", {}).get("events", [])
            ]
            st.dataframe(rows, use_container_width=True)

            st.subheader("🔒 Compliance Records")
            for rec in out.get("compliance", {}).get("compliance_records", []):
                icon = "✅" if rec.get("status") == "PASSED" else "❌" if rec.get("status") == "FAILED" else "⏳"
                st.write(f"{icon} `{rec.get('rule_id')}` v{rec.get('rule_version')} — {rec.get('status')}")

            integ = out["integrity"]
            color = "green" if integ["chain_valid"] else "red"
            st.subheader("🔐 Cryptographic Integrity")
            st.markdown(
                f"Events verified: **{integ['events_verified']}** &nbsp;|&nbsp; "
                f"Chain valid: **:{color}[{'✓' if integ['chain_valid'] else '✗'}]** &nbsp;|&nbsp; "
                f"Tamper detected: **{integ['tamper_detected']}** &nbsp;|&nbsp; "
                f"Hash: `{integ['hash']}`"
            )
        else:
            st.error(out.get("error"))

# ─── Tab 2 · Concurrency ───────────────────────────────────────────────────────

with tab_occ:
    st.header("Step 2 — Optimistic Concurrency Control (OCC)")
    st.caption(
        "Two agents simultaneously append at expected_version=3. "
        "Exactly one must succeed. The other must receive OptimisticConcurrencyError."
    )
    if st.button("▶ Run live OCC test", key="occ_run"):
        with st.spinner("Spawning two concurrent agents…"):
            out = run_async(run_concurrency_test())
        if out.get("ok"):
            a = out["assertions"]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total events", out["total_events"],
                      delta="✅ = 4" if a["total_events_is_4"] else "❌ ≠ 4")
            c2.metric("Winning version", out["winning_version"] or "—",
                      delta="✅ = 4" if a["winning_position_is_4"] else "❌")
            c3.metric("Successes", len(out["successes"]),
                      delta="✅ = 1" if a["exactly_one_wins"] else "❌")
            c4.metric("OCC Errors", len(out["failures"]),
                      delta="✅ = 1" if a["exactly_one_fails"] else "❌")
            if all(a.values()):
                st.success("✅ All 4 assertions pass — OCC correctly enforced.")
            else:
                st.error("❌ One or more assertions failed.")

            with st.expander("Details"):
                st.json(out)
        else:
            st.error(out.get("error"))

# ─── Tab 3 · Temporal Query ────────────────────────────────────────────────────

with tab_temporal:
    st.header("Step 3 — Temporal Compliance Query")
    st.caption("Show compliance state as it existed at a specific point in time.")
    c1, c2 = st.columns(2)
    with c1:
        t_app = st.selectbox("Application ID", SEED_APPS, key="tq_app")
    with c2:
        as_of = st.text_input("As-of timestamp (ISO 8601)", "2026-03-16T12:00:00Z", key="tq_ts")
    if st.button("▶ Query temporal state", key="tq_run"):
        with st.spinner("Querying compliance at timestamp…"):
            out = run_async(run_temporal_query(t_app, as_of))
        if out.get("ok"):
            col_a, col_b = st.columns(2)
            with col_a:
                st.subheader(f"🕐 At {as_of[:10]}")
                recs = out["at_time"].get("compliance_records", [])
                if recs:
                    for rec in recs:
                        icon = "✅" if rec.get("status") == "PASSED" else "❌"
                        st.write(f"{icon} `{rec.get('rule_id')}` — {rec.get('status')}")
                else:
                    st.info("No compliance records at this timestamp.")
            with col_b:
                st.subheader("📅 Current state")
                for rec in out["current"].get("compliance_records", []):
                    icon = "✅" if rec.get("status") == "PASSED" else "❌"
                    st.write(f"{icon} `{rec.get('rule_id')}` — {rec.get('status')}")
        else:
            st.error(out.get("error"))

# ─── Tab 4 · Upcasting Demo ────────────────────────────────────────────────────

with tab_upcast:
    st.header("Step 4 — Upcasting & Immutability")
    st.caption(
        "A v1 CreditAnalysisCompleted event is loaded through the UpcasterRegistry — it arrives as v2. "
        "The raw database row is unchanged."
    )
    u_app = st.selectbox("Application ID", SEED_APPS, key="up_app")
    if st.button("▶ Run upcasting demo", key="up_run"):
        with st.spinner("Loading event through upcasting registry…"):
            out = run_async(run_upcasting_demo(u_app))
        if out.get("ok"):
            c1, c2 = st.columns(2)
            with c1:
                st.subheader("📦 Loaded via EventStore (upcasted)")
                st.metric("Event version (in-memory)", out["loaded_version"])
                st.caption("Keys: " + ", ".join(f"`{k}`" for k in out["loaded_keys"]))
                st.json(out["upcasted_payload"])
            with c2:
                st.subheader("🗄️ Raw DB row (stored)")
                st.metric("Event version (in DB)", out["raw_db_version"])
                st.caption("Keys: " + ", ".join(f"`{k}`" for k in out["raw_db_keys"]))
                st.json(out["raw_db_payload"])
            if out["immutable"]:
                st.success(
                    "✅ Immutability confirmed: stored payload differs from upcasted payload — "
                    "the raw DB row was NOT modified."
                )
            else:
                st.warning("⚠ Payloads are identical — event may already be v2 in the store.")
        else:
            st.error(out.get("error"))
            if len(out.keys()) > 1:
                st.json(out)

# ─── Tab 5 · Gas Town ──────────────────────────────────────────────────────────

with tab_gas:
    st.header("Step 5 — Gas Town: Agent Memory Recovery")
    st.caption(
        "Start an agent session, append 5 events, then call reconstruct_agent_context() "
        "without the in-memory agent object (simulated crash)."
    )

    gas_mode = st.radio(
        "Gas Town mode",
        ["Simulated crash (append new events)", "Replay stored agent session (use seeded app)"],
        index=1,
        key="gas_mode",
    )

    if gas_mode == "Replay stored agent session (use seeded app)":
        gas_app = st.selectbox("Application ID", SEED_APPS, key="gas_app")
        if st.button("▶ Replay stored session", key="gas_replay_run"):
            with st.spinner(f"Reconstructing Gas Town context for {gas_app}…"):
                out = run_async(run_gas_town_replay_for_app(gas_app))
            if out.get("ok"):
                c1, c2, c3 = st.columns(3)
                c1.metric("Session ID", out["session_id"])
                c2.metric("Last event position", out["last_event_position"])
                c3.metric("Session health", out["session_health"])

                st.subheader("🔁 Reconstructed Context (preview)")
                st.code(out["context_preview"], language="text")

                st.write("**Pending work:**", out["pending_work"] or "None")
                st.write("**Model version:**", out["model_version"])
                st.success(
                    f"✅ Agent `{out['agent_id']}` can resume from position "
                    f"{out['last_event_position']} — reconstructed from stored events."
                )
            else:
                st.error(out.get("error"))
    else:
        if st.button("▶ Run Gas Town demo", key="gas_run"):
            with st.spinner("Appending 5 events → simulating crash → reconstructing context…"):
                out = run_async(run_gas_town_demo())
            if out.get("ok"):
                c1, c2, c3 = st.columns(3)
                c1.metric("Events appended", out["events_appended"])
                c2.metric("Last event position", out["last_event_position"])
                c3.metric("Session health", out["session_health"])

                st.subheader("🔁 Reconstructed Context (preview)")
                st.code(out["context_preview"], language="text")

                st.write("**Pending work:**", out["pending_work"] or "None")
                st.write("**Model version:**", out["model_version"])
                st.success(
                    f"✅ Agent `{out['agent_id']}` can resume from position "
                    f"{out['last_event_position']} — no completed work repeated."
                )
            else:
                st.error(out.get("error"))

# ─── Tab 6 · What-If ───────────────────────────────────────────────────────────

with tab_whatif:
    st.header("Step 6 (Bonus) — What-If Counterfactual Analysis")
    st.caption(
        "What would the final decision have been if the credit analysis had returned "
        "a different risk tier?"
    )
    c1, c2 = st.columns(2)
    with c1:
        wi_app = st.selectbox("Application ID", SEED_APPS, key="wi_app")
    with c2:
        risk_tier = st.selectbox("Counterfactual risk tier", ["HIGH", "MEDIUM", "LOW"], key="wi_risk")
    if st.button("▶ Run What-If", key="wi_run"):
        with st.spinner("Running counterfactual projection…"):
            out = run_async(run_what_if_demo(wi_app, risk_tier))
        if out.get("ok"):
            st.info(
                f"Branch: **CreditAnalysisCompleted** · "
                f"Original risk tier: `{out['original_risk_tier']}` → "
                f"Counterfactual: `{out['counterfactual_risk_tier']}`"
            )
            col_r, col_cf = st.columns(2)
            real = out.get("real_outcome") or {}
            cf = out.get("counterfactual_outcome") or {}
            with col_r:
                st.subheader("📊 Real Outcome")
                st.metric("Final State", real.get("final_state", "?"))
                st.metric("Risk Tier", real.get("risk_tier", "?"))
                st.metric("Decision", real.get("decision", "?"))
                if real.get("confidence_score") is not None:
                    st.metric("Confidence", f"{real['confidence_score']:.2f}")
            with col_cf:
                st.subheader(f"🔀 Counterfactual ({risk_tier})")
                st.metric("Final State", cf.get("final_state", "?"))
                st.metric("Risk Tier", cf.get("risk_tier", "?"))
                st.metric("Decision", cf.get("decision", "?"))
                if cf.get("confidence_score") is not None:
                    st.metric("Confidence", f"{cf['confidence_score']:.2f}")

            divs = out.get("divergence_events", [])
            if divs:
                st.warning(f"⚡ {len(divs)} divergence(s) detected:")
                for d in divs:
                    st.write(f"  **{d['field']}**: `{d['real']}` → `{d['counterfactual']}`")
            else:
                st.info("✓ No divergence — counterfactual produces same outcome.")

            st.caption(
                f"Events replayed (real): {out['events_replayed_real']} · "
                f"(counterfactual): {out['events_replayed_cf']} · "
                f"Computed in {out['elapsed_ms']:.1f}ms"
            )
        else:
            st.error(out.get("error"))

# ─── Tab 7 · Metrics ───────────────────────────────────────────────────────────

with tab_metrics:
    st.header("📊 Agent Performance & Projection Health")
    if st.button("▶ Refresh metrics", key="m_run"):
        with st.spinner("Loading metrics…"):
            out = run_async(load_metrics())
        if out.get("ok"):
            # Projection lags
            lags = out["health"].get("projection_lags_ms", {})
            if lags:
                st.subheader("💓 Projection Lags")
                cols = st.columns(len(lags))
                for i, (name, lag_ms) in enumerate(lags.items()):
                    slo = 2000 if "compliance" in name else 500
                    cols[i].metric(name, f"{lag_ms} ms",
                                   delta="✅ SLO" if lag_ms < slo else "⚠ Over SLO")

            # Agent performance table
            st.subheader("🤖 Agent Performance Ledger")
            if out["agent_rows"]:
                import pandas as pd  # local import — optional dep

                df = pd.DataFrame(out["agent_rows"])
                display = [
                    c for c in [
                        "agent_id", "model_version",
                        "analyses_completed", "decisions_generated",
                        "avg_confidence_score", "avg_duration_ms",
                        "approve_rate", "decline_rate", "refer_rate",
                    ] if c in df.columns
                ]
                st.dataframe(df[display], use_container_width=True)
            else:
                st.info("No agent performance data yet. Run agents first.")

            # Application state breakdown
            st.subheader("📋 Applications by State")
            if out["app_states"]:
                for row in out["app_states"]:
                    st.write(f"  **{row.get('state', '?')}**: {row.get('n', 0)}")
            else:
                st.info("No application data. Seed and run agents first.")
        else:
            st.error(out.get("error"))

# ─── Tab 8 · Run Agents ────────────────────────────────────────────────────────

with tab_run:
    st.header("🚀 Run Full Agent Pipeline")
    st.caption(
        "Runs DocumentProcessing → CreditAnalysis → FraudDetection → Compliance → "
        "DecisionOrchestrator agents sequentially for a given application ID."
    )
    c1, c2 = st.columns(2)
    with c1:
        run_app = st.text_input("Application ID", value="APEX-0030", key="run_app")
    with c2:
        run_model = st.text_input("Model", value="claude-sonnet-4-20250514", key="run_model")
    if st.button("▶ Run pipeline", key="run_btn"):
        with st.spinner(f"Running all 5 agents for {run_app}… (60–120s)"):
            result = run_pipeline(run_app, run_model)
        if result.get("ok"):
            st.success("Pipeline completed successfully.")
            st.code(result.get("output", ""), language="text")
        else:
            st.error(result.get("error", "Unknown error"))
            if result.get("output"):
                st.code(result["output"], language="text")

# ─── Tab 9 · Setup ─────────────────────────────────────────────────────────────

with tab_setup:
    st.header("⚙️ Setup")
    st.caption("Run database migrations and/or seed the event store from JSONL.")

    if st.button("Run migrations", key="setup_migrate"):
        with st.spinner("Running migrations…"):
            out = run_async(run_migrations_async())
        if out.get("ok"):
            st.success(out.get("message"))
        else:
            st.error(out.get("error"))

    default_seed = str(
        Path(__file__).resolve().parent.parent / "starter" / "data" / "seed_events.jsonl"
    )
    seed_path = st.text_input("Seed JSONL path", value=default_seed, key="seed_path")
    if st.button("Seed from JSONL", key="setup_seed"):
        with st.spinner("Seeding…"):
            out = seed_from_jsonl(seed_path)
        if out.get("ok"):
            st.success(out.get("message"))
        else:
            st.error(out.get("error"))


# ─── Tab 10 · Assessment Evidence Pack ───────────────────────────────────────

with tab_evidence:
    st.header("🧾 Assessment Evidence Pack")
    st.caption(
        "Generates rubric-ready evidence artifacts. In single-app mode you can "
        "run the real LLM agent pipeline, then render DB-derived evidence "
        "(event timeline, API cost, regulatory JSON)."
    )

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Evidence generation mode")
        mode = st.radio(
            "Choose whether to generate evidence for one application or the full seeded set.",
            ["Single application", "All seeded applications"],
            index=0,
            key="evidence_mode",
        )

        if mode == "Single application":
            evidence_model = st.text_input(
                "LLM model (real generation only)",
                value="claude-sonnet-4-20250514",
                key="evidence_llm_model",
            )
            selected_app = st.selectbox(
                "Application ID",
                SEED_APPS,
                index=0,
                key="evidence_single_app",
            )
            if st.button(
                "Run real agents + generate evidence (selected app)", key="evidence_gen_single"
            ):
                with st.spinner(f"Generating artifacts for {selected_app}…"):
                    gen_out = generate_real_llm_evidence(selected_app, evidence_model)
                if gen_out.get("ok"):
                    st.session_state["evidence_single"] = gen_out
                    st.success(f"Real evidence generated for {selected_app}.")
                else:
                    st.error(gen_out.get("error") or "Real evidence generation failed.")

            if st.button(
                "Generate rubric evidence from stored events only (no agent rerun)",
                key="evidence_gen_single_db",
            ):
                with st.spinner(f"Generating DB-derived evidence for {selected_app}…"):
                    gen_out = generate_real_db_evidence(selected_app, model=evidence_model)
                if gen_out.get("ok"):
                    st.session_state["evidence_single"] = gen_out
                    st.success(f"DB-derived evidence generated for {selected_app}.")
                else:
                    st.error(gen_out.get("error") or "DB-derived evidence generation failed.")

            if st.button("Load artifacts (selected app)", key="evidence_load_single"):
                with st.spinner(f"Loading artifacts for {selected_app}…"):
                    loaded = load_week5_artifacts(selected_app)
                st.session_state["evidence_single"] = loaded
                if loaded.get("ok"):
                    st.success("Loaded artifacts.")
                else:
                    st.error(loaded.get("error") or "Failed to load artifacts.")

        else:
            st.subheader("Evidence artifacts for all seeded apps (optional)")
            st.caption(f"Seeded application IDs: {', '.join(SEED_APPS)}")

            if st.button("Generate evidence pack (all apps)", key="evidence_gen_all"):
                with st.spinner("Generating narratives + API cost once, then regulatory packages for all apps…"):
                    first_app = SEED_APPS[0]
                    gen_out = generate_week5_assessment_artifacts(first_app)
                    if not gen_out.get("ok"):
                        st.error(gen_out.get("error") or "Artifact generation failed.")
                        st.stop()

                    for app_id in SEED_APPS[1:]:
                        generate_regulatory_package_artifact(app_id)

                    st.success("Generated evidence artifacts for all seeded applications.")

            if st.button("Load global artifacts", key="evidence_load_all"):
                with st.spinner("Loading global artifacts from artifacts/…"):
                    glob = load_week5_global_artifacts()
                st.session_state["evidence_global"] = glob
                if glob.get("ok"):
                    st.success("Loaded global evidence artifacts.")
                else:
                    st.error("Failed to load global artifacts.")
                    st.error(json.dumps(glob.get("errors") or {}, default=str, indent=2))

    with col_b:
        st.subheader("Verification tests")
        if st.button("Run MCP lifecycle test", key="evidence_run_mcp"):
            with st.spinner("Running tests/test_mcp_lifecycle.py…"):
                out = run_pytest_capture("tests/test_mcp_lifecycle.py", timeout_s=600)
            if out.get("ok"):
                st.success("MCP lifecycle test passed.")
            else:
                st.error(out.get("error") or "MCP lifecycle test failed.")
            st.code((out.get("output") or "").strip()[:20_000], language="text")

        if st.button("Run projection SLO tests", key="evidence_run_projections"):
            with st.spinner("Running tests/test_projections.py…"):
                out = run_pytest_capture("tests/test_projections.py", timeout_s=600)
            if out.get("ok"):
                st.success("Projection SLO tests passed.")
            else:
                st.error(out.get("error") or "Projection SLO tests failed.")
            st.code((out.get("output") or "").strip()[:20_000], language="text")

    evidence_single = st.session_state.get("evidence_single")
    if mode == "Single application" and evidence_single and evidence_single.get("ok"):
        st.divider()
        st.subheader("Real agent narrative (event timeline)")
        if evidence_single.get("timeline", {}).get("output"):
            st.code((evidence_single["timeline"]["output"] or "").strip()[:25_000], language="text")
        else:
            # Backward compatibility with deterministic artifacts.
            st.code((evidence_single.get("narrative_test_results") or "").strip()[:25_000], language="text")

        st.subheader("Rubric narrative (LLM from DB)")
        if evidence_single.get("rubric_narrative_text"):
            st.code((evidence_single["rubric_narrative_text"] or "").strip()[:25_000], language="text")
        else:
            rubric = evidence_single.get("rubric_narrative") or {}
            st.info(rubric.get("error") or "Rubric narrative not generated/loaded yet.")

        st.subheader("API cost report")
        if evidence_single.get("api_cost_report_text"):
            st.code((evidence_single.get("api_cost_report_text") or "").strip()[:25_000], language="text")
        else:
            st.code((evidence_single.get("api_cost_report") or "").strip()[:25_000], language="text")

        st.subheader("Regulatory package (JSON)")
        if evidence_single.get("regulatory_package_json") is not None:
            st.json(evidence_single["regulatory_package_json"])
        elif evidence_single.get("regulatory_package") is not None:
            st.json(evidence_single["regulatory_package"])
        else:
            st.error(evidence_single.get("regulatory_package_error") or "Regulatory package missing/unreadable.")

        st.subheader("Artifact generation log")
        log_text = (
            evidence_single.get("artifact_generation_log")
            or (evidence_single.get("pipeline") or {}).get("output")
            or ""
        )
        st.code((log_text or "").strip()[:20_000], language="text")

    glob_loaded = st.session_state.get("evidence_global")
    if mode != "Single application" and glob_loaded and glob_loaded.get("ok"):
        st.divider()
        st.subheader("Narrative test results")
        st.code((glob_loaded.get("narrative_test_results") or "").strip()[:25_000], language="text")

        st.subheader("API cost report")
        st.code((glob_loaded.get("api_cost_report") or "").strip()[:25_000], language="text")

        st.subheader("Artifact generation log")
        st.code((glob_loaded.get("artifact_generation_log") or "").strip()[:20_000], language="text")

        artifacts_dir = Path(__file__).resolve().parent.parent / "artifacts"
        available_regulatory = []
        for app_id in SEED_APPS:
            reg_path = artifacts_dir / f"regulatory_package_{app_id}.json"
            if reg_path.exists():
                available_regulatory.append(app_id)

        if available_regulatory:
            st.subheader("Regulatory packages (JSON)")
            selected_app = st.selectbox(
                "Select an application to view its regulatory package",
                available_regulatory,
                index=0,
                key="evidence_regulatory_select",
            )
            loaded = load_week5_artifacts(selected_app)
            if loaded.get("ok"):
                if loaded.get("regulatory_package") is not None:
                    st.json(loaded["regulatory_package"])
                else:
                    st.error(loaded.get("regulatory_package_error") or "Regulatory package missing/unreadable.")
        else:
            st.info("No regulatory packages found yet. Click “Generate evidence pack (all apps)”.")
