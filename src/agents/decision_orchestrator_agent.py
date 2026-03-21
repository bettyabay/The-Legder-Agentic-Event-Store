from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from src.agents.base_agent import BaseApexAgent


class OrchestratorState(TypedDict):
    application_id: str
    session_id: str
    agent_id: str
    credit_result: dict[str, Any] | None
    fraud_result: dict[str, Any] | None
    compliance_result: dict[str, Any] | None
    recommendation: str | None
    confidence: float | None
    approved_amount: float | None
    executive_summary: str | None
    key_risks: list[str]
    conditions: list[str]
    hard_constraints_applied: list[str]
    output_events_written: list[dict[str, Any]]
    next_agent_triggered: str | None


class DecisionOrchestratorAgent(BaseApexAgent):
    """Week-5 decision orchestrator: synthesize + enforce hard constraints."""

    def build_graph(self) -> Any:
        graph = StateGraph(OrchestratorState)
        graph.add_node("validate_inputs", self._node_validate_inputs)
        graph.add_node("load_credit_result", self._node_load_credit)
        graph.add_node("load_fraud_result", self._node_load_fraud)
        graph.add_node("load_compliance_result", self._node_load_compliance)
        graph.add_node("synthesize_decision", self._node_synthesize)
        graph.add_node("apply_hard_constraints", self._node_constraints)
        graph.add_node("write_output", self._node_write_output)

        graph.set_entry_point("validate_inputs")
        graph.add_edge("validate_inputs", "load_credit_result")
        graph.add_edge("load_credit_result", "load_fraud_result")
        graph.add_edge("load_fraud_result", "load_compliance_result")
        graph.add_edge("load_compliance_result", "synthesize_decision")
        graph.add_edge("synthesize_decision", "apply_hard_constraints")
        graph.add_edge("apply_hard_constraints", "write_output")
        graph.add_edge("write_output", END)
        return graph.compile()

    def _initial_state(self) -> dict[str, Any]:
        state = super()._initial_state()
        state.update(
            {
                "credit_result": None,
                "fraud_result": None,
                "compliance_result": None,
                "recommendation": None,
                "confidence": None,
                "approved_amount": None,
                "executive_summary": None,
                "key_risks": [],
                "conditions": [],
                "hard_constraints_applied": [],
                "output_events_written": [],
                "next_agent_triggered": None,
            }
        )
        return state

    async def _node_validate_inputs(self, state: OrchestratorState) -> OrchestratorState:
        t0 = time.time()
        app_id = state["application_id"]
        loan_events = await self.store.load_stream(f"loan-{app_id}")
        if not any(e.event_type == "ApplicationSubmitted" for e in loan_events):
            raise ValueError(f"ApplicationSubmitted not found for {app_id}")
        await self._record_node_execution(
            node_name="validate_inputs",
            input_keys=["application_id"],
            output_keys=["application_id"],
            duration_ms=int((time.time() - t0) * 1000),
        )
        return state

    async def _node_load_credit(self, state: OrchestratorState) -> OrchestratorState:
        t0 = time.time()
        app_id = state["application_id"]
        credit_events = await self.store.load_stream(f"credit-{app_id}")
        credit = next((e.payload for e in reversed(credit_events) if e.event_type == "CreditAnalysisCompleted"), None)
        if credit is None:
            raise ValueError(f"CreditAnalysisCompleted not found for {app_id}")
        ms = int((time.time() - t0) * 1000)
        await self._record_tool_call(
            tool_name="load_event_store_stream",
            tool_input_summary=f"credit-{app_id}",
            tool_output_summary="CreditAnalysisCompleted found",
            tool_duration_ms=ms,
        )
        await self._record_node_execution(
            node_name="load_credit_result",
            input_keys=["application_id"],
            output_keys=["credit_result"],
            duration_ms=ms,
        )
        return {**state, "credit_result": credit}

    async def _node_load_fraud(self, state: OrchestratorState) -> OrchestratorState:
        t0 = time.time()
        app_id = state["application_id"]
        events = await self.store.load_stream(f"fraud-{app_id}")
        fraud = next((e.payload for e in reversed(events) if e.event_type == "FraudScreeningCompleted"), None)
        if fraud is None:
            raise ValueError(f"FraudScreeningCompleted not found for {app_id}")
        ms = int((time.time() - t0) * 1000)
        await self._record_tool_call(
            tool_name="load_event_store_stream",
            tool_input_summary=f"fraud-{app_id}",
            tool_output_summary="FraudScreeningCompleted found",
            tool_duration_ms=ms,
        )
        await self._record_node_execution(
            node_name="load_fraud_result",
            input_keys=["application_id"],
            output_keys=["fraud_result"],
            duration_ms=ms,
        )
        return {**state, "fraud_result": fraud}

    async def _node_load_compliance(self, state: OrchestratorState) -> OrchestratorState:
        t0 = time.time()
        app_id = state["application_id"]
        events = await self.store.load_stream(f"compliance-{app_id}")
        compliance = next((e.payload for e in reversed(events) if e.event_type == "ComplianceCheckCompleted"), None)
        if compliance is None:
            raise ValueError(f"ComplianceCheckCompleted not found for {app_id}")
        ms = int((time.time() - t0) * 1000)
        await self._record_tool_call(
            tool_name="load_event_store_stream",
            tool_input_summary=f"compliance-{app_id}",
            tool_output_summary="ComplianceCheckCompleted found",
            tool_duration_ms=ms,
        )
        await self._record_node_execution(
            node_name="load_compliance_result",
            input_keys=["application_id"],
            output_keys=["compliance_result"],
            duration_ms=ms,
        )
        return {**state, "compliance_result": compliance}

    async def _node_synthesize(self, state: OrchestratorState) -> OrchestratorState:
        t0 = time.time()
        credit = state.get("credit_result") or {}
        decision = credit.get("decision") or {}
        fraud = state.get("fraud_result") or {}
        compliance = state.get("compliance_result") or {}

        system = (
            "You are a senior loan officer. Return ONLY JSON with keys: "
            "recommendation(APPROVE|DECLINE|REFER), confidence(0..1), approved_amount_usd(number), "
            "executive_summary(string), key_risks(array), conditions(array)."
        )
        user = (
            f"Credit: {json.dumps(credit, default=str)[:1800]}\n"
            f"Fraud: {json.dumps(fraud, default=str)[:1200]}\n"
            f"Compliance: {json.dumps(compliance, default=str)[:1200]}\n"
        )
        llm = await self._call_llm(system, user, max_tokens=600)
        try:
            match = re.search(r"\{.*\}", llm.text, re.DOTALL)
            parsed = json.loads(match.group(0) if match else "{}")
        except json.JSONDecodeError:
            parsed = {}

        recommendation = str(parsed.get("recommendation") or "REFER").upper()
        confidence = float(parsed.get("confidence") or decision.get("confidence") or 0.65)
        approved_amount = float(parsed.get("approved_amount_usd") or decision.get("recommended_limit_usd") or 0.0)
        summary = str(parsed.get("executive_summary") or "Decision synthesized from credit, fraud, and compliance.")
        key_risks = parsed.get("key_risks") or []
        conditions = parsed.get("conditions") or []

        await self._record_node_execution(
            node_name="synthesize_decision",
            input_keys=["credit_result", "fraud_result", "compliance_result"],
            output_keys=["recommendation", "confidence", "approved_amount", "executive_summary"],
            duration_ms=int((time.time() - t0) * 1000),
            llm_called=True,
            llm_tokens_input=llm.input_tokens,
            llm_tokens_output=llm.output_tokens,
            llm_cost_usd=llm.cost_usd,
        )
        return {
            **state,
            "recommendation": recommendation,
            "confidence": confidence,
            "approved_amount": max(0.0, approved_amount),
            "executive_summary": summary,
            "key_risks": key_risks if isinstance(key_risks, list) else [str(key_risks)],
            "conditions": conditions if isinstance(conditions, list) else [str(conditions)],
        }

    async def _node_constraints(self, state: OrchestratorState) -> OrchestratorState:
        t0 = time.time()
        recommendation = str(state.get("recommendation") or "REFER")
        confidence = float(state.get("confidence") or 0.0)
        fraud_score = float((state.get("fraud_result") or {}).get("fraud_score") or 0.0)
        verdict = str((state.get("compliance_result") or {}).get("overall_verdict") or "CONDITIONAL")
        risk_tier = str(((state.get("credit_result") or {}).get("decision") or {}).get("risk_tier") or "MEDIUM")

        applied: list[str] = []
        if verdict == "BLOCKED":
            recommendation = "DECLINE"
            applied.append("compliance_blocked_forced_decline")
        if confidence < 0.60:
            recommendation = "REFER"
            applied.append("low_confidence_forced_refer")
        if fraud_score > 0.60:
            recommendation = "REFER"
            applied.append("high_fraud_forced_refer")
        if risk_tier == "HIGH" and confidence < 0.70:
            recommendation = "REFER"
            applied.append("high_risk_low_conf_forced_refer")

        await self._record_node_execution(
            node_name="apply_hard_constraints",
            input_keys=["recommendation", "confidence", "fraud_result", "compliance_result", "credit_result"],
            output_keys=["recommendation", "hard_constraints_applied"],
            duration_ms=int((time.time() - t0) * 1000),
        )
        return {**state, "recommendation": recommendation, "hard_constraints_applied": applied}

    async def _node_write_output(self, state: OrchestratorState) -> OrchestratorState:
        t0 = time.time()
        app_id = state["application_id"]
        loan_stream = f"loan-{app_id}"
        loan_ver = await self.store.stream_version(loan_stream)

        rec = str(state.get("recommendation") or "REFER")
        conf = float(state.get("confidence") or 0.0)
        approved_amount = float(state.get("approved_amount") or 0.0)
        decision_event = {
            "event_type": "DecisionGenerated",
            "event_version": 2,
            "payload": {
                "application_id": app_id,
                "orchestrator_session_id": self.session_id,
                "recommendation": rec,
                "confidence": conf,
                "approved_amount_usd": approved_amount if rec == "APPROVE" else None,
                "conditions": state.get("conditions") or [],
                "executive_summary": state.get("executive_summary") or "",
                "key_risks": state.get("key_risks") or [],
                "contributing_sessions": [
                    (state.get("credit_result") or {}).get("session_id"),
                    (state.get("fraud_result") or {}).get("session_id"),
                    (state.get("compliance_result") or {}).get("session_id"),
                ],
                "model_versions": {"orchestrator": self.model},
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        new_ver = await self.store.append(loan_stream, [decision_event], expected_version=loan_ver)
        current_ver = new_ver
        events_written = [{"stream_id": loan_stream, "event_type": "DecisionGenerated", "stream_position": new_ver}]

        if rec == "APPROVE":
            current_ver = await self.store.append(
                loan_stream,
                [
                    {
                        "event_type": "ApplicationApproved",
                        "event_version": 1,
                        "payload": {
                            "application_id": app_id,
                            "approved_amount_usd": approved_amount,
                            "interest_rate_pct": 8.25,
                            "term_months": 36,
                            "conditions": state.get("conditions") or [],
                            "approved_by": "decision_orchestrator",
                            "effective_date": datetime.now(timezone.utc).date().isoformat(),
                            "approved_at": datetime.now(timezone.utc).isoformat(),
                        },
                    }
                ],
                expected_version=current_ver,
            )
            events_written.append({"stream_id": loan_stream, "event_type": "ApplicationApproved", "stream_position": current_ver})
            next_agent = None
        elif rec == "DECLINE":
            current_ver = await self.store.append(
                loan_stream,
                [
                    {
                        "event_type": "ApplicationDeclined",
                        "event_version": 1,
                        "payload": {
                            "application_id": app_id,
                            "decline_reasons": state.get("key_risks") or ["Risk profile not acceptable."],
                            "declined_by": "decision_orchestrator",
                            "adverse_action_notice_required": True,
                            "adverse_action_codes": ["DECLINE-RISK"],
                            "declined_at": datetime.now(timezone.utc).isoformat(),
                        },
                    }
                ],
                expected_version=current_ver,
            )
            events_written.append({"stream_id": loan_stream, "event_type": "ApplicationDeclined", "stream_position": current_ver})
            next_agent = None
        else:
            current_ver = await self.store.append(
                loan_stream,
                [
                    {
                        "event_type": "HumanReviewRequested",
                        "event_version": 1,
                        "payload": {
                            "application_id": app_id,
                            "reason": "Low confidence or policy constraint triggered REFER.",
                            "decision_event_id": f"loan-pos-{new_ver}",
                            "assigned_to": None,
                            "requested_at": datetime.now(timezone.utc).isoformat(),
                        },
                    }
                ],
                expected_version=current_ver,
            )
            events_written.append({"stream_id": loan_stream, "event_type": "HumanReviewRequested", "stream_position": current_ver})
            next_agent = "human_review"

        await self._record_output_written(events_written=events_written, output_summary=f"Orchestrator wrote final recommendation={rec}.")
        await self._record_node_execution(
            node_name="write_output",
            input_keys=["recommendation", "confidence", "approved_amount", "key_risks", "conditions"],
            output_keys=["output_events_written", "next_agent_triggered"],
            duration_ms=int((time.time() - t0) * 1000),
        )
        return {**state, "output_events_written": events_written, "next_agent_triggered": next_agent}
