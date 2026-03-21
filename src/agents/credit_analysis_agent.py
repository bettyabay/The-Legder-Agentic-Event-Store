from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from src.agents.base_agent import BaseApexAgent


class CreditState(TypedDict):
    application_id: str
    session_id: str
    agent_id: str
    applicant_id: str | None
    requested_amount_usd: float | None
    loan_purpose: str | None
    company_profile: dict[str, Any] | None
    historical_financials: list[dict[str, Any]] | None
    compliance_flags: list[dict[str, Any]] | None
    loan_history: list[dict[str, Any]] | None
    credit_decision: dict[str, Any] | None
    output_events_written: list[dict[str, Any]]
    next_agent_triggered: str | None


class CreditAnalysisAgent(BaseApexAgent):
    """Week-5 credit analysis agent with LLM call + policy caps."""

    def build_graph(self) -> Any:
        graph = StateGraph(CreditState)
        graph.add_node("validate_inputs", self._node_validate_inputs)
        graph.add_node("load_applicant_registry", self._node_load_registry)
        graph.add_node("analyze_credit_risk", self._node_analyze)
        graph.add_node("write_output", self._node_write_output)

        graph.set_entry_point("validate_inputs")
        graph.add_edge("validate_inputs", "load_applicant_registry")
        graph.add_edge("load_applicant_registry", "analyze_credit_risk")
        graph.add_edge("analyze_credit_risk", "write_output")
        graph.add_edge("write_output", END)
        return graph.compile()

    def _initial_state(self) -> dict[str, Any]:
        state = super()._initial_state()
        state.update(
            {
                "applicant_id": None,
                "requested_amount_usd": None,
                "loan_purpose": None,
                "company_profile": None,
                "historical_financials": None,
                "compliance_flags": None,
                "loan_history": None,
                "credit_decision": None,
                "output_events_written": [],
                "next_agent_triggered": None,
            }
        )
        return state

    async def _node_validate_inputs(self, state: CreditState) -> CreditState:
        t0 = time.time()
        app_id = state["application_id"]
        loan_events = await self.store.load_stream(f"loan-{app_id}")

        submitted = next((e for e in loan_events if e.event_type == "ApplicationSubmitted"), None)
        if submitted is None:
            raise ValueError(f"ApplicationSubmitted not found for {app_id}")

        payload = submitted.payload
        next_state: CreditState = {
            **state,
            "applicant_id": str(payload.get("applicant_id", "")),
            "requested_amount_usd": float(payload.get("requested_amount_usd") or 0.0),
            "loan_purpose": str(payload.get("loan_purpose", "unknown")),
        }

        await self._record_node_execution(
            node_name="validate_inputs",
            input_keys=["application_id"],
            output_keys=["applicant_id", "requested_amount_usd", "loan_purpose"],
            duration_ms=int((time.time() - t0) * 1000),
        )
        return next_state

    async def _node_load_registry(self, state: CreditState) -> CreditState:
        t0 = time.time()
        applicant_id = state["applicant_id"]
        if not applicant_id:
            raise ValueError("Missing applicant_id")

        company = await self.registry.get_company(applicant_id)
        financials = await self.registry.get_financial_history(applicant_id)
        flags = await self.registry.get_compliance_flags(applicant_id)
        loans = await self.registry.get_loan_relationships(applicant_id)

        ms = int((time.time() - t0) * 1000)
        await self._record_tool_call(
            tool_name="query_applicant_registry",
            tool_input_summary=f"company_id={applicant_id}",
            tool_output_summary=(
                f"profile={'yes' if company else 'no'}, "
                f"financial_rows={len(financials)}, flags={len(flags)}, loans={len(loans)}"
            ),
            tool_duration_ms=ms,
        )
        await self._record_node_execution(
            node_name="load_applicant_registry",
            input_keys=["applicant_id"],
            output_keys=["company_profile", "historical_financials", "compliance_flags", "loan_history"],
            duration_ms=ms,
        )

        return {
            **state,
            "company_profile": self._to_json_dict(company) if company else None,
            "historical_financials": [self._to_json_dict(row) for row in financials],
            "compliance_flags": [self._to_json_dict(flag) for flag in flags],
            "loan_history": loans,
        }

    @staticmethod
    def _to_json_dict(obj: Any) -> dict[str, Any]:
        """Support both Pydantic v2 (model_dump) and v1 (dict)."""
        if is_dataclass(obj):
            return asdict(obj)
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        if hasattr(obj, "dict"):
            return obj.dict()
        if isinstance(obj, dict):
            return obj
        return dict(obj)

    async def _node_analyze(self, state: CreditState) -> CreditState:
        t0 = time.time()
        req_amt = float(state.get("requested_amount_usd") or 0.0)
        company = state.get("company_profile") or {}
        financials = state.get("historical_financials") or []
        flags = state.get("compliance_flags") or []
        loans = state.get("loan_history") or []

        latest_revenue = float(financials[-1].get("total_revenue") or 0.0) if financials else 0.0
        hard_cap = latest_revenue * 0.35 if latest_revenue > 0 else req_amt
        prior_default = any(bool(l.get("default_occurred")) for l in loans)
        has_high_flag = any(
            str(f.get("severity", "")).upper() == "HIGH" and bool(f.get("is_active", False))
            for f in flags
        )

        system = (
            "You are a commercial credit analyst. Return ONLY JSON with keys: "
            "risk_tier (LOW|MEDIUM|HIGH), recommended_limit_usd (number), confidence (0..1), "
            "rationale (string), key_concerns (array of strings), data_quality_caveats (array)."
        )
        user = (
            f"Applicant: {company.get('name', 'Unknown')}\n"
            f"Requested amount: {req_amt}\n"
            f"Loan purpose: {state.get('loan_purpose')}\n"
            f"Latest annual revenue: {latest_revenue}\n"
            f"Financial history rows: {len(financials)}\n"
            f"Compliance flags: {json.dumps(flags)[:1200]}\n"
            f"Loan history: {json.dumps(loans)[:1200]}\n"
        )
        llm = await self._call_llm(system, user, max_tokens=500)

        decision: dict[str, Any]
        try:
            match = re.search(r"\{.*\}", llm.text, re.DOTALL)
            decision = json.loads(match.group(0) if match else "{}")
        except json.JSONDecodeError:
            decision = {}

        recommended_limit = float(decision.get("recommended_limit_usd") or req_amt * 0.8)
        confidence = float(decision.get("confidence") or 0.7)
        risk_tier = str(decision.get("risk_tier") or "MEDIUM").upper()
        if hard_cap > 0:
            recommended_limit = min(recommended_limit, hard_cap)
        if prior_default:
            risk_tier = "HIGH"
        if has_high_flag:
            confidence = min(confidence, 0.5)

        decision["recommended_limit_usd"] = round(recommended_limit, 2)
        decision["confidence"] = round(confidence, 4)
        decision["risk_tier"] = risk_tier
        decision.setdefault("rationale", "Generated via LLM credit analysis.")
        decision.setdefault("key_concerns", [])
        decision.setdefault("data_quality_caveats", [])

        await self._record_node_execution(
            node_name="analyze_credit_risk",
            input_keys=["historical_financials", "compliance_flags", "loan_history"],
            output_keys=["credit_decision"],
            duration_ms=int((time.time() - t0) * 1000),
            llm_called=True,
            llm_tokens_input=llm.input_tokens,
            llm_tokens_output=llm.output_tokens,
            llm_cost_usd=llm.cost_usd,
        )
        return {**state, "credit_decision": decision}

    async def _node_write_output(self, state: CreditState) -> CreditState:
        t0 = time.time()
        app_id = state["application_id"]
        decision = state.get("credit_decision") or {}

        credit_stream = f"credit-{app_id}"
        credit_ver = await self.store.stream_version(credit_stream)

        credit_events: list[dict[str, Any]] = []
        if credit_ver == 0:
            credit_events.append(
                {
                    "event_type": "CreditRecordOpened",
                    "event_version": 1,
                    "payload": {
                        "application_id": app_id,
                        "applicant_id": state.get("applicant_id"),
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                    },
                }
            )
            expected = -1
        else:
            expected = credit_ver

        credit_events.append(
            {
                "event_type": "CreditAnalysisCompleted",
                "event_version": 2,
                "payload": {
                    "application_id": app_id,
                    "session_id": self.session_id,
                    "decision": decision,
                    "model_version": self.model,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        )
        new_credit_ver = await self._append_with_occ(
            stream_id=credit_stream,
            events=credit_events,
            expected_version=expected,
        )

        loan_stream = f"loan-{app_id}"
        loan_ver = await self.store.stream_version(loan_stream)
        new_loan_ver = await self._append_with_occ(
            stream_id=loan_stream,
            events=[
                {
                    "event_type": "FraudScreeningRequested",
                    "event_version": 1,
                    "payload": {
                        "application_id": app_id,
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                        "triggered_by_event_id": f"credit-pos-{new_credit_ver}",
                    },
                }
            ],
            expected_version=loan_ver,
        )

        events_written = [
            {"stream_id": credit_stream, "event_type": "CreditAnalysisCompleted", "stream_position": new_credit_ver},
            {"stream_id": loan_stream, "event_type": "FraudScreeningRequested", "stream_position": new_loan_ver},
        ]
        await self._record_output_written(
            events_written=events_written,
            output_summary=f"Credit analysis written; requested fraud screening for {app_id}.",
        )
        await self._record_node_execution(
            node_name="write_output",
            input_keys=["credit_decision"],
            output_keys=["output_events_written", "next_agent_triggered"],
            duration_ms=int((time.time() - t0) * 1000),
        )
        return {
            **state,
            "output_events_written": events_written,
            "next_agent_triggered": "fraud_detection",
        }
