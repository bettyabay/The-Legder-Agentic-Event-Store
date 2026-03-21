from __future__ import annotations

import argparse
import asyncio

from starter.ledger.schema.events import AgentType

from src.agents.compliance_agent import ComplianceAgent
from src.agents.credit_analysis_agent import CreditAnalysisAgent
from src.agents.decision_orchestrator_agent import DecisionOrchestratorAgent
from src.agents.document_processing_agent import DocumentProcessingAgent
from src.agents.fraud_detection_agent import FraudDetectionAgent
from src.db.pool import create_pool, run_migrations
from src.event_store import EventStore
from src.llm.client import create_async_anthropic_client
from src.registry.client import ApplicantRegistryClient


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Run full src Week-5 agent chain for one application.")
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--model", default="claude-sonnet-4-20250514")
    args = parser.parse_args()

    pool = await create_pool()
    try:
        await run_migrations(pool)
        store = EventStore(pool=pool)
        registry = ApplicantRegistryClient(pool)
        client = create_async_anthropic_client()
        app_id = args.application_id

        doc = DocumentProcessingAgent(
            agent_id="doc-agent-1",
            agent_type=AgentType.DOCUMENT_PROCESSING,
            store=store,
            registry=registry,
            client=client,
            model=args.model,
        )
        credit = CreditAnalysisAgent(
            agent_id="credit-agent-1",
            agent_type=AgentType.CREDIT_ANALYSIS,
            store=store,
            registry=registry,
            client=client,
            model=args.model,
        )
        fraud = FraudDetectionAgent(
            agent_id="fraud-agent-1",
            agent_type=AgentType.FRAUD_DETECTION,
            store=store,
            registry=registry,
            client=client,
            model=args.model,
        )
        compliance = ComplianceAgent(
            agent_id="compliance-agent-1",
            agent_type=AgentType.COMPLIANCE,
            store=store,
            registry=registry,
            client=client,
            model=args.model,
        )
        decision = DecisionOrchestratorAgent(
            agent_id="decision-agent-1",
            agent_type=AgentType.DECISION_ORCHESTRATOR,
            store=store,
            registry=registry,
            client=client,
            model=args.model,
        )

        await doc.process_application(app_id)
        await credit.process_application(app_id)
        await fraud.process_application(app_id)
        await compliance.process_application(app_id)
        await decision.process_application(app_id)

        print(f"OK: full src pipeline completed for {app_id}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(_main())
