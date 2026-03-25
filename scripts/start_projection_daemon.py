from __future__ import annotations
import asyncio
from src.db.pool import create_pool, run_migrations
from src.event_store import EventStore
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon

async def main() -> None:
    pool = await create_pool()
    await run_migrations(pool)
    store = EventStore(pool=pool)
    daemon = ProjectionDaemon(
        store=store,
        projections=[
            ApplicationSummaryProjection(),
            AgentPerformanceLedgerProjection(),
            ComplianceAuditViewProjection(pool=pool),
        ],
        pool=pool, )
    print("Projection daemon started")
    await daemon.run_forever(poll_interval_ms=100)

if __name__ == "__main__":
    asyncio.run(main())