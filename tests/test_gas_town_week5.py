from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from src.event_store import EventStore
from src.integrity.gas_town import reconstruct_agent_context
from src.models.events import SessionHealth


@pytest.mark.asyncio
async def test_gas_town_reconstructs_week5_session_node_progress(db_pool) -> None:
    """
    Week-5 canonical session stream uses:
      - AgentSessionStarted (first)
      - AgentNodeExecuted (per node)
      - AgentSessionFailed (crash anchor)

    Gas Town must:
      - locate the stream by session_id
      - derive session health from a failed session
      - expose last_successful_node from the last AgentNodeExecuted
    """
    store = EventStore(pool=db_pool)

    # Import canonical Week-5 event types
    from starter.ledger.schema.events import (
        AgentNodeExecuted,
        AgentSessionFailed,
        AgentSessionStarted,
        AgentType,
    )

    session_id = f"session-{uuid4().hex[:8]}"
    agent_id = f"agent-{uuid4().hex[:6]}"
    app_id = f"app-{uuid4().hex[:6]}"

    agent_type = AgentType.FRAUD_DETECTION
    stream_id = f"agent-{agent_type.value}-{session_id}"

    now = datetime.now(tz=timezone.utc)

    await store.append(
        stream_id,
        [
            AgentSessionStarted(
                session_id=session_id,
                agent_type=agent_type,
                agent_id=agent_id,
                application_id=app_id,
                model_version="fraud-v1.2",
                langgraph_graph_version="1.0.0",
                context_source="fresh",
                context_token_count=4200,
                started_at=now,
            ),
            AgentNodeExecuted(
                session_id=session_id,
                agent_type=agent_type,
                node_name="load_facts",
                node_sequence=1,
                input_keys=["application_id"],
                output_keys=["historical_financials"],
                llm_called=False,
                llm_tokens_input=None,
                llm_tokens_output=None,
                llm_cost_usd=None,
                duration_ms=12,
                executed_at=now,
            ),
            AgentSessionFailed(
                session_id=session_id,
                agent_type=agent_type,
                agent_id=agent_id,
                application_id=app_id,
                error_type="SimulatedCrash",
                error_message="boom",
                last_successful_node="load_facts",
                recoverable=True,
                failed_at=now,
            ),
        ],
        expected_version=-1,
    )

    ctx = await reconstruct_agent_context(
        store=store,
        agent_id=agent_id,
        session_id=session_id,
    )

    assert ctx.session_health_status == SessionHealth.NEEDS_RECONCILIATION
    assert ctx.last_successful_node == "load_facts"
    assert "load_facts" in ctx.executed_nodes
    assert ctx.context_text != ""

