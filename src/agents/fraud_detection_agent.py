from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from src.agents.base_agent import BaseApexAgent


class FraudState(TypedDict):
    application_id: str
    session_id: str
    agent_id: str
    applicant_id: str | None
    extracted_facts: dict[str, Any] | None
    historical_financials: list[dict[str, Any]] | None
    company_profile: dict[str, Any] | None
    anomalies: list[dict[str, Any]]
    fraud_score: float | None
    recommendation: str | None
    output_events_written: list[dict[str, Any]]
    next_agent_triggered: str | None


class FraudDetectionAgent(BaseApexAgent):
    """Week-5 fraud screening agent with LLM-assisted anomaly scoring."""

    def build_graph(self) -> Any:
        graph = StateGraph(FraudState)
        graph.add_node("validate_inputs", self._node_validate_inputs)
        graph.add_node("load_document_facts", self._node_load_facts)
        graph.add_node("cross_reference_registry", self._node_cross_reference)
        graph.add_node("analyze_fraud_patterns", self._node_analyze)
        graph.add_node("write_output", self._node_write_output)

        graph.set_entry_point("validate_inputs")
        graph.add_edge("validate_inputs", "load_document_facts")
        graph.add_edge("load_document_facts", "cross_reference_registry")
        graph.add_edge("cross_reference_registry", "analyze_fraud_patterns")
        graph.add_edge("analyze_fraud_patterns", "write_output")
        graph.add_edge("write_output", END)
        return graph.compile()

    def _initial_state(self) -> dict[str, Any]:
        state = super()._initial_state()
        state.update(
            {
                "applicant_id": None,
                "extracted_facts": None,
                "historical_financials": None,
                "company_profile": None,
                "anomalies": [],
                "fraud_score": None,
                "recommendation": None,
                "output_events_written": [],
                "next_agent_triggered": None,
            }
        )
        return state

    async def _node_validate_inputs(self, state: FraudState) -> FraudState:
        t0 = time.time()
        app_id = state["application_id"]
        loan_events = await self.store.load_stream(f"loan-{app_id}")
        submitted = next((e for e in loan_events if e.event_type == "ApplicationSubmitted"), None)
        if submitted is None:
            raise ValueError(f"ApplicationSubmitted not found for {app_id}")
        applicant_id = str(submitted.payload.get("applicant_id", ""))
        await self._record_node_execution(
            node_name="validate_inputs",
            input_keys=["application_id"],
            output_keys=["applicant_id"],
            duration_ms=int((time.time() - t0) * 1000),
        )
        return {**state, "applicant_id": applicant_id}

    async def _node_load_facts(self, state: FraudState) -> FraudState:
        t0 = time.time()
        app_id = state["application_id"]
        doc_events = await self.store.load_stream(f"docpkg-{app_id}")
        extraction_events = [e for e in doc_events if e.event_type == "ExtractionCompleted"]
        merged: dict[str, Any] = {}
        for ev in extraction_events:
            facts = ev.payload.get("facts") or {}
            for k, v in facts.items():
                if v is not None and k not in merged:
                    merged[k] = v

        ms = int((time.time() - t0) * 1000)
        await self._record_tool_call(
            tool_name="load_event_store_stream",
            tool_input_summary=f"docpkg-{app_id}",
            tool_output_summary=f"ExtractionCompleted={len(extraction_events)}",
            tool_duration_ms=ms,
        )
        await self._record_node_execution(
            node_name="load_document_facts",
            input_keys=["application_id"],
            output_keys=["extracted_facts"],
            duration_ms=ms,
        )
        return {**state, "extracted_facts": merged}

    async def _node_cross_reference(self, state: FraudState) -> FraudState:
        t0 = time.time()
        applicant_id = state.get("applicant_id")
        if not applicant_id:
            raise ValueError("Missing applicant_id")
        company = await self.registry.get_company(applicant_id)
        financials = await self.registry.get_financial_history(applicant_id)
        ms = int((time.time() - t0) * 1000)
        await self._record_tool_call(
            tool_name="query_applicant_registry",
            tool_input_summary=f"company_id={applicant_id}",
            tool_output_summary=f"profile={'yes' if company else 'no'}, financial_rows={len(financials)}",
            tool_duration_ms=ms,
        )
        await self._record_node_execution(
            node_name="cross_reference_registry",
            input_keys=["applicant_id", "extracted_facts"],
            output_keys=["company_profile", "historical_financials"],
            duration_ms=ms,
        )
        return {
            **state,
            "company_profile": self._to_json_dict(company) if company else None,
            "historical_financials": [self._to_json_dict(r) for r in financials],
        }

    async def _node_analyze(self, state: FraudState) -> FraudState:
        t0 = time.time()
        extracted = state.get("extracted_facts") or {}
        financials = state.get("historical_financials") or []
        current_revenue = float(extracted.get("total_revenue") or 0.0)
        prior_revenue = float(financials[-1].get("total_revenue") or 0.0) if financials else 0.0
        revenue_gap = abs(current_revenue - prior_revenue) / prior_revenue if prior_revenue > 0 else 0.0

        system = (
            "You are a fraud analyst. Return ONLY JSON with keys: fraud_score (0..1), "
            "recommendation (PROCEED|FLAG_FOR_REVIEW|DECLINE), anomalies (array of objects with "
            "anomaly_type, description, severity, evidence)."
        )
        user = (
            f"Extracted facts: {json.dumps(extracted, default=str)[:1500]}\n"
            f"Historical financials: {json.dumps(financials[-3:], default=str)[:1500]}\n"
            f"Computed revenue_gap_ratio={revenue_gap:.4f}\n"
        )
        llm = await self._call_llm(system, user, max_tokens=500)
        try:
            match = re.search(r"\{.*\}", llm.text, re.DOTALL)
            parsed = json.loads(match.group(0) if match else "{}")
        except json.JSONDecodeError:
            parsed = {}

        anomalies = parsed.get("anomalies") or []
        fraud_score = float(parsed.get("fraud_score") or 0.05)
        if revenue_gap > 0.40:
            fraud_score = max(fraud_score, 0.45)
            if not anomalies:
                anomalies = [
                    {
                        "anomaly_type": "revenue_discrepancy",
                        "description": "Large discrepancy between extracted and historical revenue.",
                        "severity": "MEDIUM",
                        "evidence": f"gap_ratio={round(revenue_gap,4)}",
                    }
                ]
        fraud_score = max(0.0, min(fraud_score, 1.0))
        if fraud_score > 0.60:
            recommendation = "DECLINE"
        elif fraud_score >= 0.30:
            recommendation = "FLAG_FOR_REVIEW"
        else:
            recommendation = "PROCEED"

        await self._record_node_execution(
            node_name="analyze_fraud_patterns",
            input_keys=["extracted_facts", "historical_financials"],
            output_keys=["anomalies", "fraud_score", "recommendation"],
            duration_ms=int((time.time() - t0) * 1000),
            llm_called=True,
            llm_tokens_input=llm.input_tokens,
            llm_tokens_output=llm.output_tokens,
            llm_cost_usd=llm.cost_usd,
        )
        return {
            **state,
            "anomalies": anomalies,
            "fraud_score": fraud_score,
            "recommendation": recommendation,
        }

    async def _node_write_output(self, state: FraudState) -> FraudState:
        t0 = time.time()
        app_id = state["application_id"]
        fraud_stream = f"fraud-{app_id}"
        fraud_ver = await self.store.stream_version(fraud_stream)
        expected = -1 if fraud_ver == 0 else fraud_ver

        events: list[dict[str, Any]] = []
        if expected == -1:
            events.append(
                {
                    "event_type": "FraudScreeningInitiated",
                    "event_version": 1,
                    "payload": {
                        "application_id": app_id,
                        "session_id": self.session_id,
                        "screening_model_version": self.model,
                        "initiated_at": datetime.now(timezone.utc).isoformat(),
                    },
                }
            )
        for anomaly in state.get("anomalies") or []:
            events.append(
                {
                    "event_type": "FraudAnomalyDetected",
                    "event_version": 1,
                    "payload": {
                        "application_id": app_id,
                        "session_id": self.session_id,
                        "anomaly": anomaly,
                        "detected_at": datetime.now(timezone.utc).isoformat(),
                    },
                }
            )
        events.append(
            {
                "event_type": "FraudScreeningCompleted",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "session_id": self.session_id,
                    "fraud_score": state.get("fraud_score"),
                    "risk_level": "HIGH" if (state.get("fraud_score") or 0) > 0.60 else "MEDIUM",
                    "anomalies_found": len(state.get("anomalies") or []),
                    "recommendation": state.get("recommendation"),
                    "screening_model_version": self.model,
                    "input_data_hash": self._sha(
                        {"facts": state.get("extracted_facts"), "hist": state.get("historical_financials")}
                    ),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        )
        new_fraud_ver = await self.store.append(fraud_stream, events, expected_version=expected)

        loan_stream = f"loan-{app_id}"
        loan_ver = await self.store.stream_version(loan_stream)
        new_loan_ver = await self.store.append(
            loan_stream,
            [
                {
                    "event_type": "ComplianceCheckRequested",
                    "event_version": 1,
                    "payload": {
                        "application_id": app_id,
                        "regulation_set_version": "REG-2026-Q1",
                        "checks_required": ["REG-001", "REG-002", "REG-003", "REG-004", "REG-005", "REG-006"],
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                        "triggered_by_event_id": f"fraud-pos-{new_fraud_ver}",
                    },
                }
            ],
            expected_version=loan_ver,
        )

        events_written = [
            {"stream_id": fraud_stream, "event_type": "FraudScreeningCompleted", "stream_position": new_fraud_ver},
            {"stream_id": loan_stream, "event_type": "ComplianceCheckRequested", "stream_position": new_loan_ver},
        ]
        await self._record_output_written(
            events_written=events_written,
            output_summary=f"Fraud screening completed; compliance requested for {app_id}.",
        )
        await self._record_node_execution(
            node_name="write_output",
            input_keys=["anomalies", "fraud_score", "recommendation"],
            output_keys=["output_events_written", "next_agent_triggered"],
            duration_ms=int((time.time() - t0) * 1000),
        )
        return {
            **state,
            "output_events_written": events_written,
            "next_agent_triggered": "compliance",
        }

    @staticmethod
    def _to_json_dict(obj: Any) -> dict[str, Any]:
        if is_dataclass(obj):
            return asdict(obj)
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        if hasattr(obj, "dict"):
            return obj.dict()
        if isinstance(obj, dict):
            return obj
        return dict(obj)
