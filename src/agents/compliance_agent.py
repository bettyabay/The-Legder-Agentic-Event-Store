from __future__ import annotations

import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from src.agents.base_agent import BaseApexAgent


class ComplianceState(TypedDict):
    application_id: str
    session_id: str
    agent_id: str
    applicant_id: str | None
    requested_amount_usd: float | None
    company_profile: dict[str, Any] | None
    compliance_flags: list[dict[str, Any]] | None
    has_hard_block: bool
    hard_block_rule: str | None
    rules_evaluated: int
    rules_passed: int
    rules_failed: int
    rules_noted: int
    failure_reasons: list[str]
    output_events_written: list[dict[str, Any]]
    next_agent_triggered: str | None


class ComplianceAgent(BaseApexAgent):
    """Week-5 deterministic compliance agent (no LLM in rule path)."""

    RULES: list[dict[str, Any]] = [
        {
            "id": "REG-001",
            "name": "Bank Secrecy Act Check",
            "version": "2026-Q1-v1",
            "hard_block": False,
            "eval": lambda s: not any(
                f.get("flag_type") == "AML_WATCH" and bool(f.get("is_active"))
                for f in (s.get("compliance_flags") or [])
            ),
            "fail_reason": "Active AML watch flag detected.",
            "remediation": "Enhanced due diligence package required.",
        },
        {
            "id": "REG-002",
            "name": "OFAC Sanctions Screening",
            "version": "2026-Q1-v1",
            "hard_block": True,
            "eval": lambda s: not any(
                f.get("flag_type") == "SANCTIONS_REVIEW" and bool(f.get("is_active"))
                for f in (s.get("compliance_flags") or [])
            ),
            "fail_reason": "Sanctions review active.",
            "remediation": None,
        },
        {
            "id": "REG-003",
            "name": "Jurisdiction Lending Eligibility",
            "version": "2026-Q1-v1",
            "hard_block": True,
            "eval": lambda s: (s.get("company_profile") or {}).get("jurisdiction") != "MT",
            "fail_reason": "REG-003 hard block: jurisdiction MT is not eligible.",
            "remediation": None,
        },
        {
            "id": "REG-004",
            "name": "Legal Entity Type Eligibility",
            "version": "2026-Q1-v1",
            "hard_block": False,
            "eval": lambda s: not (
                (s.get("company_profile") or {}).get("legal_type") == "Sole Proprietor"
                and float(s.get("requested_amount_usd") or 0.0) > 250_000
            ),
            "fail_reason": "Sole Proprietor above 250k requires enhanced docs.",
            "remediation": "Provide personal financial statement and guarantor package.",
        },
        {
            "id": "REG-005",
            "name": "Minimum Operating History",
            "version": "2026-Q1-v1",
            "hard_block": True,
            "eval": lambda s: (2026 - int((s.get("company_profile") or {}).get("founded_year") or 2026)) >= 2,
            "fail_reason": "Business operating history is below 2 years.",
            "remediation": None,
        },
    ]

    def build_graph(self) -> Any:
        graph = StateGraph(ComplianceState)
        graph.add_node("validate_inputs", self._node_validate_inputs)
        graph.add_node("load_company_profile", self._node_load_profile)
        graph.add_node("evaluate_rules", self._node_evaluate_rules)
        graph.add_node("write_output", self._node_write_output)

        graph.set_entry_point("validate_inputs")
        graph.add_edge("validate_inputs", "load_company_profile")
        graph.add_edge("load_company_profile", "evaluate_rules")
        graph.add_edge("evaluate_rules", "write_output")
        graph.add_edge("write_output", END)
        return graph.compile()

    def _initial_state(self) -> dict[str, Any]:
        state = super()._initial_state()
        state.update(
            {
                "applicant_id": None,
                "requested_amount_usd": None,
                "company_profile": None,
                "compliance_flags": None,
                "has_hard_block": False,
                "hard_block_rule": None,
                "rules_evaluated": 0,
                "rules_passed": 0,
                "rules_failed": 0,
                "rules_noted": 0,
                "failure_reasons": [],
                "output_events_written": [],
                "next_agent_triggered": None,
            }
        )
        return state

    async def _node_validate_inputs(self, state: ComplianceState) -> ComplianceState:
        t0 = time.time()
        app_id = state["application_id"]
        loan_events = await self.store.load_stream(f"loan-{app_id}")
        submitted = next((e for e in loan_events if e.event_type == "ApplicationSubmitted"), None)
        if submitted is None:
            raise ValueError(f"ApplicationSubmitted not found for {app_id}")
        await self._record_node_execution(
            node_name="validate_inputs",
            input_keys=["application_id"],
            output_keys=["applicant_id", "requested_amount_usd"],
            duration_ms=int((time.time() - t0) * 1000),
        )
        return {
            **state,
            "applicant_id": str(submitted.payload.get("applicant_id", "")),
            "requested_amount_usd": float(submitted.payload.get("requested_amount_usd") or 0.0),
        }

    async def _node_load_profile(self, state: ComplianceState) -> ComplianceState:
        t0 = time.time()
        applicant_id = state.get("applicant_id")
        if not applicant_id:
            raise ValueError("Missing applicant_id")
        company = await self.registry.get_company(applicant_id)
        flags = await self.registry.get_compliance_flags(applicant_id)
        ms = int((time.time() - t0) * 1000)
        await self._record_tool_call(
            tool_name="query_applicant_registry",
            tool_input_summary=f"company_id={applicant_id}",
            tool_output_summary=f"profile={'yes' if company else 'no'}, flags={len(flags)}",
            tool_duration_ms=ms,
        )
        await self._record_node_execution(
            node_name="load_company_profile",
            input_keys=["applicant_id"],
            output_keys=["company_profile", "compliance_flags"],
            duration_ms=ms,
        )
        return {
            **state,
            "company_profile": self._to_json_dict(company) if company else None,
            "compliance_flags": [self._to_json_dict(f) for f in flags],
        }

    async def _node_evaluate_rules(self, state: ComplianceState) -> ComplianceState:
        t0 = time.time()
        app_id = state["application_id"]
        comp_stream = f"compliance-{app_id}"
        current_ver = await self.store.stream_version(comp_stream)
        expected = -1 if current_ver == 0 else current_ver

        events: list[dict[str, Any]] = []
        if expected == -1:
            events.append(
                {
                    "event_type": "ComplianceCheckInitiated",
                    "event_version": 1,
                    "payload": {
                        "application_id": app_id,
                        "session_id": self.session_id,
                        "regulation_set_version": "2026-Q1",
                        "rules_to_evaluate": [r["id"] for r in self.RULES] + ["REG-006"],
                        "initiated_at": datetime.now(timezone.utc).isoformat(),
                    },
                }
            )

        rules_eval = 0
        rules_passed = 0
        rules_failed = 0
        rules_noted = 0
        has_hard_block = False
        hard_block_rule: str | None = None
        failure_reasons: list[str] = []

        for rule in self.RULES:
            passed = bool(rule["eval"](state))
            rules_eval += 1
            if passed:
                rules_passed += 1
                events.append(
                    {
                        "event_type": "ComplianceRulePassed",
                        "event_version": 1,
                        "payload": {
                            "application_id": app_id,
                            "session_id": self.session_id,
                            "rule_id": rule["id"],
                            "rule_name": rule["name"],
                            "rule_version": rule["version"],
                            "evidence_hash": self._sha(state.get("company_profile")),
                            "evaluation_notes": "Passed deterministic rule evaluation.",
                            "evaluated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    }
                )
            else:
                rules_failed += 1
                failure_reasons.append(str(rule["fail_reason"]))
                is_hard = bool(rule["hard_block"])
                events.append(
                    {
                        "event_type": "ComplianceRuleFailed",
                        "event_version": 1,
                        "payload": {
                            "application_id": app_id,
                            "session_id": self.session_id,
                            "rule_id": rule["id"],
                            "rule_name": rule["name"],
                            "rule_version": rule["version"],
                            "failure_reason": rule["fail_reason"],
                            "is_hard_block": is_hard,
                            "remediation_available": rule["remediation"] is not None,
                            "remediation_description": rule["remediation"],
                            "evidence_hash": self._sha(state.get("company_profile")),
                            "evaluated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    }
                )
                if is_hard:
                    has_hard_block = True
                    hard_block_rule = str(rule["id"])
                    break

        # REG-006 informational note only if no hard-block interruption happened before it.
        if not has_hard_block:
            rules_eval += 1
            rules_noted += 1
            events.append(
                {
                    "event_type": "ComplianceRuleNoted",
                    "event_version": 1,
                    "payload": {
                        "application_id": app_id,
                        "session_id": self.session_id,
                        "rule_id": "REG-006",
                        "rule_name": "CRA Community Reinvestment Act",
                        "note_type": "CRA_CONSIDERATION",
                        "note_text": "Informational CRA consideration recorded.",
                        "evaluated_at": datetime.now(timezone.utc).isoformat(),
                    },
                }
            )

        overall_verdict = "BLOCKED" if has_hard_block else ("CONDITIONAL" if rules_failed > 0 else "CLEAR")
        events.append(
            {
                "event_type": "ComplianceCheckCompleted",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "session_id": self.session_id,
                    "rules_evaluated": rules_eval,
                    "rules_passed": rules_passed,
                    "rules_failed": rules_failed,
                    "rules_noted": rules_noted,
                    "has_hard_block": has_hard_block,
                    "overall_verdict": overall_verdict,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        )

        new_ver = await self.store.append(comp_stream, events, expected_version=expected)
        await self._record_node_execution(
            node_name="evaluate_rules",
            input_keys=["company_profile", "compliance_flags", "requested_amount_usd"],
            output_keys=["has_hard_block", "rules_evaluated", "rules_failed"],
            duration_ms=int((time.time() - t0) * 1000),
        )
        return {
            **state,
            "has_hard_block": has_hard_block,
            "hard_block_rule": hard_block_rule,
            "rules_evaluated": rules_eval,
            "rules_passed": rules_passed,
            "rules_failed": rules_failed,
            "rules_noted": rules_noted,
            "failure_reasons": failure_reasons,
            "output_events_written": [
                {"stream_id": comp_stream, "event_type": "ComplianceCheckCompleted", "stream_position": new_ver}
            ],
        }

    async def _node_write_output(self, state: ComplianceState) -> ComplianceState:
        t0 = time.time()
        app_id = state["application_id"]
        loan_stream = f"loan-{app_id}"
        loan_ver = await self.store.stream_version(loan_stream)

        if state.get("has_hard_block"):
            payload = {
                "application_id": app_id,
                "decline_reasons": state.get("failure_reasons") or ["Compliance hard block."],
                "declined_by": "compliance_agent",
                "adverse_action_notice_required": True,
                "adverse_action_codes": [state.get("hard_block_rule") or "REG-UNKNOWN"],
                "declined_at": datetime.now(timezone.utc).isoformat(),
            }
            event_type = "ApplicationDeclined"
            next_agent = None
        else:
            payload = {
                "application_id": app_id,
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "all_analyses_complete": True,
                "triggered_by_event_id": f"compliance-{self.session_id}",
            }
            event_type = "DecisionRequested"
            next_agent = "decision_orchestrator"

        new_loan_ver = await self.store.append(
            loan_stream,
            [{"event_type": event_type, "event_version": 1, "payload": payload}],
            expected_version=loan_ver,
        )
        events_written = [
            {"stream_id": loan_stream, "event_type": event_type, "stream_position": new_loan_ver},
        ]
        await self._record_output_written(
            events_written=events_written,
            output_summary=f"{event_type} written for {app_id}.",
        )
        await self._record_node_execution(
            node_name="write_output",
            input_keys=["has_hard_block", "failure_reasons"],
            output_keys=["output_events_written", "next_agent_triggered"],
            duration_ms=int((time.time() - t0) * 1000),
        )
        return {**state, "output_events_written": events_written, "next_agent_triggered": next_agent}

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
