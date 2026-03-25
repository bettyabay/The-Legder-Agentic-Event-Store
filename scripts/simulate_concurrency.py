from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from uuid import uuid4
from src.db.pool import create_pool, run_migrations
from src.event_store import EventStore
from src.models.events import ApplicationSubmitted, CreditAnalysisCompleted
from src.models.exceptions import OptimisticConcurrencyError

async def main():
    pool = await create_pool()
    await run_migrations(pool)
    store = EventStore(pool=pool)
    app_id = f"qa-conc-{uuid4().hex[:8]}"
    stream_id = f"loan-{app_id}"
    now = datetime.now(tz=timezone.utc)

    await store.append(stream_id, [ApplicationSubmitted(
        application_id=app_id, applicant_id="qa", requested_amount_usd=100000,
        loan_purpose="test", submission_channel="api", submitted_at=now
    )], expected_version=-1)

    v = await store.stream_version(stream_id)
    ev = CreditAnalysisCompleted(
        application_id=app_id, agent_id="agent-a", session_id="s1", model_version="v1",
        confidence_score=0.7, risk_tier="MEDIUM", recommended_limit_usd=80000,
        analysis_duration_ms=1000, input_data_hash="h"
    )


    async def attempt(name):
        try:
            nv = await store.append(stream_id, [ev], expected_version=v)
            print(name, "WIN", nv)
        except OptimisticConcurrencyError as e:
            print(name, "LOSE", e.expected_version, e.actual_version)

    await asyncio.gather(attempt("A"), attempt("B"))
    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())