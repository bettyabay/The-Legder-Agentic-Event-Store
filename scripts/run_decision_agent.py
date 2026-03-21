from __future__ import annotations

import argparse
import asyncio

from starter.ledger.schema.events import AgentType

from src.agents.decision_orchestrator_agent import DecisionOrchestratorAgent
from src.db.pool import create_pool, run_migrations
from src.event_store import EventStore
from src.llm.client import create_async_anthropic_client
from src.registry.client import ApplicantRegistryClient


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Run Week-5 DecisionOrchestratorAgent for one application.")
    parser.add_argument("--application-id", required=True, help="Application ID, e.g. APEX-0012")
    parser.add_argument("--agent-id", default="decision-agent-1")
    parser.add_argument("--model", default="claude-sonnet-4-20250514")
    args = parser.parse_args()

    pool = await create_pool()
    try:
        await run_migrations(pool)
        store = EventStore(pool=pool)
        registry = ApplicantRegistryClient(pool)
        client = create_async_anthropic_client()

        agent = DecisionOrchestratorAgent(
            agent_id=args.agent_id,
            agent_type=AgentType.DECISION_ORCHESTRATOR,
            store=store,
            registry=registry,
            client=client,
            model=args.model,
        )
        await agent.process_application(args.application_id)
        print(f"OK: decision agent completed for {args.application_id}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(_main())
