from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from starter.ledger.schema.events import AgentType

from src.agents.compliance_agent import ComplianceAgent
from src.agents.credit_analysis_agent import CreditAnalysisAgent
from src.agents.decision_orchestrator_agent import DecisionOrchestratorAgent
from src.agents.document_processing_agent import DocumentProcessingAgent
from src.agents.fraud_detection_agent import FraudDetectionAgent
from src.agents.base_agent import LlmCallResult
from src.event_store import EventStore


@dataclass
class FakeCompany:
    company_id: str
    name: str = "Narrative Co"
    industry: str = "manufacturing"
    naics: str = "000000"
    jurisdiction: str = "CA"
    legal_type: str = "LLC"
    founded_year: int = 2010
    employee_count: int = 70
    risk_segment: str = "MEDIUM"
    trajectory: str = "STABLE"
    submission_channel: str = "web"
    ip_region: str = "US-W"


@dataclass
class FakeFinancial:
    fiscal_year: int = 2024
    total_revenue: Decimal = Decimal("3800000")
    gross_profit: Decimal = Decimal("1400000")
    operating_expenses: Decimal = Decimal("900000")
    operating_income: Decimal = Decimal("500000")
    ebitda: Decimal | None = Decimal("600000")
    depreciation_amortization: Decimal = Decimal("100000")
    interest_expense: Decimal = Decimal("80000")
    income_before_tax: Decimal = Decimal("420000")
    tax_expense: Decimal = Decimal("100000")
    net_income: Decimal = Decimal("320000")
    total_assets: Decimal = Decimal("5000000")
    current_assets: Decimal = Decimal("2100000")
    cash_and_equivalents: Decimal = Decimal("350000")
    accounts_receivable: Decimal = Decimal("700000")
    inventory: Decimal = Decimal("500000")
    total_liabilities: Decimal = Decimal("2200000")
    current_liabilities: Decimal = Decimal("1100000")
    long_term_debt: Decimal = Decimal("900000")
    total_equity: Decimal = Decimal("2800000")
    operating_cash_flow: Decimal = Decimal("350000")
    investing_cash_flow: Decimal = Decimal("-120000")
    financing_cash_flow: Decimal = Decimal("50000")
    free_cash_flow: Decimal = Decimal("230000")
    debt_to_equity: Decimal | None = Decimal("0.78")
    current_ratio: Decimal | None = Decimal("1.9")
    debt_to_ebitda: Decimal | None = Decimal("1.5")
    interest_coverage_ratio: Decimal | None = Decimal("5.0")
    gross_margin: Decimal | None = Decimal("0.36")
    ebitda_margin: Decimal | None = Decimal("0.16")
    net_margin: Decimal | None = Decimal("0.08")
    balance_sheet_check: bool = True


@dataclass
class FakeFlag:
    flag_type: str
    severity: str = "LOW"
    is_active: bool = True
    added_date: date = date(2026, 1, 1)
    note: str = ""


class FakeRegistry:
    def __init__(
        self,
        *,
        company: FakeCompany,
        financials: list[FakeFinancial] | None = None,
        flags: list[FakeFlag] | None = None,
        loans: list[dict] | None = None,
    ) -> None:
        self._company = company
        self._financials = financials or [FakeFinancial()]
        self._flags = flags or []
        self._loans = loans or []

    async def get_company(self, _company_id: str):
        return self._company

    async def get_financial_history(self, _company_id: str, years=None):
        return self._financials

    async def get_compliance_flags(self, _company_id: str, active_only: bool = False):
        if active_only:
            return [f for f in self._flags if f.is_active]
        return self._flags

    async def get_loan_relationships(self, _company_id: str):
        return self._loans


def _app_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


async def _seed_base_loan(store: EventStore, app_id: str, company_id: str) -> None:
    await store.append(
        f"loan-{app_id}",
        [
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "applicant_id": company_id,
                    "requested_amount_usd": 900000,
                    "loan_purpose": "expansion",
                    "submission_channel": "web",
                },
            },
            {
                "event_type": "DocumentUploaded",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "document_id": f"doc-inc-{app_id}",
                    "document_type": "income_statement",
                    "document_format": "pdf",
                    "file_path": __file__,  # existing file path for existence check
                },
            },
            {
                "event_type": "DocumentUploaded",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "document_id": f"doc-bal-{app_id}",
                    "document_type": "balance_sheet",
                    "document_format": "pdf",
                    "file_path": __file__,
                },
            },
        ],
        expected_version=-1,
    )


@pytest.mark.asyncio
async def test_narr01_concurrent_occ_collision(db_pool):
    store = EventStore(db_pool)
    app_id = _app_id("narr01")
    await _seed_base_loan(store, app_id, "COMP-031")
    reg = FakeRegistry(company=FakeCompany(company_id="COMP-031"))

    a1 = CreditAnalysisAgent(
        agent_id="credit-a",
        agent_type=AgentType.CREDIT_ANALYSIS,
        store=store,
        registry=reg,
        client=object(),
        model="test-model",
    )
    a2 = CreditAnalysisAgent(
        agent_id="credit-b",
        agent_type=AgentType.CREDIT_ANALYSIS,
        store=store,
        registry=reg,
        client=object(),
        model="test-model",
    )

    async def fake_llm(*_args, **_kwargs):
        return LlmCallResult(
            text='{"risk_tier":"MEDIUM","recommended_limit_usd":700000,"confidence":0.78,"rationale":"ok","key_concerns":[],"data_quality_caveats":[]}',
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.0001,
        )

    a1._call_llm = fake_llm  # type: ignore[attr-defined]
    a2._call_llm = fake_llm  # type: ignore[attr-defined]
    await asyncio.gather(a1.process_application(app_id), a2.process_application(app_id))

    events = await store.load_stream(f"credit-{app_id}")
    completed = [e for e in events if e.event_type == "CreditAnalysisCompleted"]
    assert len(completed) == 2


@pytest.mark.asyncio
async def test_narr02_document_extraction_failure(db_pool):
    store = EventStore(db_pool)
    app_id = _app_id("narr02")
    await _seed_base_loan(store, app_id, "COMP-044")
    fin = FakeFinancial(ebitda=None)
    reg = FakeRegistry(company=FakeCompany(company_id="COMP-044"), financials=[fin])

    doc = DocumentProcessingAgent(
        agent_id="doc",
        agent_type=AgentType.DOCUMENT_PROCESSING,
        store=store,
        registry=reg,
        client=object(),
        model="test-model",
    )
    credit = CreditAnalysisAgent(
        agent_id="credit",
        agent_type=AgentType.CREDIT_ANALYSIS,
        store=store,
        registry=reg,
        client=object(),
        model="test-model",
    )

    async def doc_llm(*_args, **_kwargs):
        return LlmCallResult(
            text='{"overall_confidence":0.7,"is_coherent":true,"anomalies":[],"critical_missing_fields":["ebitda"],"reextraction_recommended":true,"auditor_notes":"missing ebitda"}',
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.0001,
        )

    async def credit_llm(*_args, **_kwargs):
        return LlmCallResult(
            text='{"risk_tier":"MEDIUM","recommended_limit_usd":600000,"confidence":0.74,"rationale":"data caveat","key_concerns":["missing field"],"data_quality_caveats":["ebitda missing"]}',
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.0001,
        )

    doc._call_llm = doc_llm  # type: ignore[attr-defined]
    credit._call_llm = credit_llm  # type: ignore[attr-defined]
    await doc.process_application(app_id)
    await credit.process_application(app_id)

    pkg_events = await store.load_stream(f"docpkg-{app_id}")
    qa = next(e for e in pkg_events if e.event_type == "QualityAssessmentCompleted")
    assert "ebitda" in qa.payload.get("critical_missing_fields", [])

    credit_events = await store.load_stream(f"credit-{app_id}")
    comp = next(e for e in reversed(credit_events) if e.event_type == "CreditAnalysisCompleted")
    assert float(comp.payload["decision"]["confidence"]) <= 0.75


@pytest.mark.asyncio
async def test_narr03_agent_crash_recovery(db_pool):
    store = EventStore(db_pool)
    app_id = _app_id("narr03")
    await _seed_base_loan(store, app_id, "COMP-057")
    reg = FakeRegistry(company=FakeCompany(company_id="COMP-057"))

    await store.append(
        f"docpkg-{app_id}",
        [
            {
                "event_type": "ExtractionCompleted",
                "event_version": 1,
                "payload": {"package_id": app_id, "document_id": "d1", "document_type": "income_statement", "facts": {"total_revenue": "1000"}},
            }
        ],
        expected_version=-1,
    )

    crash_agent = FraudDetectionAgent(
        agent_id="fraud",
        agent_type=AgentType.FRAUD_DETECTION,
        store=store,
        registry=reg,
        client=object(),
        model="test-model",
    )
    resume_agent = FraudDetectionAgent(
        agent_id="fraud",
        agent_type=AgentType.FRAUD_DETECTION,
        store=store,
        registry=reg,
        client=object(),
        model="test-model",
    )

    async def fake_llm(*_args, **_kwargs):
        return LlmCallResult(
            text='{"fraud_score":0.2,"recommendation":"PROCEED","anomalies":[]}',
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.0001,
        )

    crash_agent._call_llm = fake_llm  # type: ignore[attr-defined]
    resume_agent._call_llm = fake_llm  # type: ignore[attr-defined]

    original = crash_agent._node_cross_reference

    async def boom(state):
        await original(state)
        raise RuntimeError("simulated crash")

    crash_agent._node_cross_reference = boom  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        await crash_agent.process_application(app_id, session_id="sess-crash")

    await resume_agent.process_application(
        app_id,
        session_id="sess-recover",
        context_source="prior_session_replay:sess-crash",
        prior_session_id="sess-crash",
    )

    fraud_events = await store.load_stream(f"fraud-{app_id}")
    completed = [e for e in fraud_events if e.event_type == "FraudScreeningCompleted"]
    assert len(completed) == 1

    session_events = await store.load_stream("agent-fraud_detection-sess-recover")
    assert any(e.event_type == "AgentSessionRecovered" for e in session_events)


@pytest.mark.asyncio
async def test_narr04_compliance_hard_block(db_pool):
    store = EventStore(db_pool)
    app_id = _app_id("narr04")
    await _seed_base_loan(store, app_id, "COMP-MT")
    reg = FakeRegistry(company=FakeCompany(company_id="COMP-MT", jurisdiction="MT"))
    agent = ComplianceAgent(
        agent_id="comp",
        agent_type=AgentType.COMPLIANCE,
        store=store,
        registry=reg,
        client=object(),
        model="test-model",
    )
    await agent.process_application(app_id)

    compliance_events = await store.load_stream(f"compliance-{app_id}")
    reg3_fail = [e for e in compliance_events if e.event_type == "ComplianceRuleFailed" and e.payload.get("rule_id") == "REG-003"]
    assert reg3_fail and bool(reg3_fail[0].payload.get("is_hard_block")) is True

    loan_events = await store.load_stream(f"loan-{app_id}")
    assert not any(e.event_type == "DecisionGenerated" for e in loan_events)
    declined = [e for e in loan_events if e.event_type == "ApplicationDeclined"]
    assert declined and bool(declined[-1].payload.get("adverse_action_notice_required")) is True


@pytest.mark.asyncio
async def test_narr05_human_override(db_pool):
    store = EventStore(db_pool)
    app_id = _app_id("narr05")
    await _seed_base_loan(store, app_id, "COMP-068")
    reg = FakeRegistry(company=FakeCompany(company_id="COMP-068", trajectory="DECLINING"))

    await store.append(
        f"credit-{app_id}",
        [
            {
                "event_type": "CreditAnalysisCompleted",
                "event_version": 2,
                "payload": {
                    "application_id": app_id,
                    "session_id": "sess-credit",
                    "decision": {"risk_tier": "HIGH", "recommended_limit_usd": 950000, "confidence": 0.82, "rationale": "high risk", "key_concerns": ["leverage"], "data_quality_caveats": []},
                    "model_version": "m",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        ],
        expected_version=-1,
    )
    await store.append(
        f"fraud-{app_id}",
        [
            {
                "event_type": "FraudScreeningCompleted",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "session_id": "sess-fraud",
                    "fraud_score": 0.2,
                    "risk_level": "MEDIUM",
                    "anomalies_found": 0,
                    "recommendation": "PROCEED",
                    "screening_model_version": "m",
                    "input_data_hash": "h",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        ],
        expected_version=-1,
    )
    await store.append(
        f"compliance-{app_id}",
        [
            {
                "event_type": "ComplianceCheckCompleted",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "session_id": "sess-comp",
                    "rules_evaluated": 6,
                    "rules_passed": 5,
                    "rules_failed": 1,
                    "rules_noted": 1,
                    "has_hard_block": False,
                    "overall_verdict": "CONDITIONAL",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        ],
        expected_version=-1,
    )

    dec = DecisionOrchestratorAgent(
        agent_id="dec",
        agent_type=AgentType.DECISION_ORCHESTRATOR,
        store=store,
        registry=reg,
        client=object(),
        model="test-model",
    )

    async def dec_llm(*_args, **_kwargs):
        return LlmCallResult(
            text='{"recommendation":"DECLINE","confidence":0.82,"approved_amount_usd":750000,"executive_summary":"decline","key_risks":["high leverage"],"conditions":["c1","c2"]}',
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.0001,
        )

    dec._call_llm = dec_llm  # type: ignore[attr-defined]
    await dec.process_application(app_id)

    loan_stream = f"loan-{app_id}"
    ver = await store.stream_version(loan_stream)
    await store.append(
        loan_stream,
        [
            {
                "event_type": "HumanReviewCompleted",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "reviewer_id": "LO-Sarah-Chen",
                    "override": True,
                    "original_recommendation": "DECLINE",
                    "final_decision": "APPROVE",
                    "override_reason": "15-year customer, prior repayment history, collateral offered",
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                },
            },
            {
                "event_type": "ApplicationApproved",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "approved_amount_usd": 750000,
                    "interest_rate_pct": 8.25,
                    "term_months": 36,
                    "conditions": [
                        "Monthly revenue reporting for 12 months",
                        "Personal guarantee from CEO",
                    ],
                    "approved_by": "LO-Sarah-Chen",
                    "effective_date": datetime.now(timezone.utc).date().isoformat(),
                    "approved_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        ],
        expected_version=ver,
    )

    loan_events = await store.load_stream(loan_stream)
    dg = next(e for e in loan_events if e.event_type == "DecisionGenerated")
    assert dg.payload["recommendation"] == "DECLINE"
    hr = next(e for e in loan_events if e.event_type == "HumanReviewCompleted")
    assert hr.payload["override"] is True
    assert hr.payload["reviewer_id"] == "LO-Sarah-Chen"
    appr = [e for e in loan_events if e.event_type == "ApplicationApproved"][-1]
    assert float(appr.payload["approved_amount_usd"]) == 750000
    assert len(appr.payload["conditions"]) == 2
