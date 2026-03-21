from __future__ import annotations

import asyncio
import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar
from uuid import uuid4

from anthropic import AsyncAnthropic
from langgraph.graph import END, StateGraph

from src.event_store import EventStore
from src.integrity.gas_town import reconstruct_agent_context
from starter.ledger.schema.events import (
    AgentInputValidated,
    AgentNodeExecuted,
    AgentOutputWritten,
    AgentSessionCompleted,
    AgentSessionFailed,
    AgentSessionRecovered,
    AgentSessionStarted,
    AgentToolCalled,
    AgentType,
)


LANGGRAPH_VERSION = "1.0.0"
MAX_OCC_RETRIES: ClassVar[int] = 5


@dataclass
class LlmCallResult:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class BaseApexAgent(ABC):
    """Base class for Week-5 LangGraph agents.

    Responsibilities:
      - Gas Town: append AgentSessionStarted before any other work
      - Per-node audit: append AgentNodeExecuted at the end of every node
      - Per-tool audit: append AgentToolCalled after every registry/event-store query
      - Crash recovery: use `reconstruct_agent_context()` to resume after the last successful node
    """

    def __init__(
        self,
        *,
        agent_id: str,
        agent_type: AgentType,
        store: EventStore,
        registry: Any,
        client: AsyncAnthropic,
        model: str,
    ) -> None:
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.store = store
        self.registry = registry
        self.client = client
        self.model = model

        self.session_id: str | None = None
        self.application_id: str | None = None

        # Audit counters
        self._seq: int = 0
        self._llm_calls: int = 0
        self._tokens_used: int = 0
        self._cost_usd: float = 0.0
        self._t0: float = 0.0

        self._graph: Any = None

    @abstractmethod
    def build_graph(self) -> Any:
        """Return a compiled LangGraph graph."""

    async def process_application(
        self,
        application_id: str,
        *,
        session_id: str | None = None,
        context_source: str = "fresh",
        prior_session_id: str | None = None,
    ) -> None:
        if self._graph is None:
            self._graph = self.build_graph()

        self.application_id = application_id
        self.session_id = session_id or f"sess-{self.agent_type.value[:3]}-{uuid4().hex[:8]}"

        # Reset counters per run
        self._seq = 0
        self._llm_calls = 0
        self._tokens_used = 0
        self._cost_usd = 0.0
        self._t0 = time.time()

        # GAS TOWN anchor
        await self._append_session_event(
            AgentSessionStarted(
                session_id=self.session_id,
                agent_type=self.agent_type,
                agent_id=self.agent_id,
                application_id=self.application_id,
                model_version=self.model,
                langgraph_graph_version=LANGGRAPH_VERSION,
                context_source=context_source,
                context_token_count=0,
                started_at=datetime.now(tz=None),
            )
        )

        # Optional crash recovery anchor
        if prior_session_id:
            ctx = await reconstruct_agent_context(
                store=self.store,
                agent_id=self.agent_id,
                session_id=prior_session_id,
            )
            await self._append_session_event(
                AgentSessionRecovered(
                    session_id=self.session_id,
                    agent_type=self.agent_type,
                    agent_id=self.agent_id,
                    application_id=self.application_id,
                    recovered_from_session_id=prior_session_id,
                    recovery_point=ctx.last_successful_node or "unknown",
                    recovered_at=datetime.now(tz=None),
                )
            )

        # Run graph
        try:
            result = await self._graph.ainvoke(self._initial_state())
            await self._complete_session(result=result)
        except Exception as e:  # noqa: BLE001
            await self._fail_session(
                error_type=type(e).__name__,
                error_message=str(e),
                last_successful_node=self._last_node_name(),
                recoverable=True,
            )
            raise

    def _initial_state(self) -> dict[str, Any]:
        assert self.application_id is not None
        assert self.session_id is not None
        return {
            "application_id": self.application_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
        }

    @property
    def session_stream_id(self) -> str:
        assert self.session_id is not None
        return f"agent-{self.agent_type.value}-{self.session_id}"

    def _last_node_name(self) -> str | None:
        # We only set this when nodes record their execution.
        return getattr(self, "_last_node_name_cache", None)

    async def _append_session_event(self, event: Any) -> None:
        # New session streams are unique; expected_version=-1 is safe.
        await self._append_with_occ(stream_id=self.session_stream_id, events=[event], expected_version=-1)

    async def _append_with_occ(self, *, stream_id: str, events: list[Any], expected_version: int) -> int:
        for attempt in range(MAX_OCC_RETRIES):
            try:
                return await self.store.append(
                    stream_id=stream_id,
                    events=events,
                    expected_version=expected_version if attempt == 0 else await self.store.stream_version(stream_id),
                    correlation_id=None,
                    causation_id=None,
                )
            except Exception as e:  # noqa: BLE001
                # OCC collisions will be retried; other exceptions propagate.
                if "OptimisticConcurrencyError" in type(e).__name__ and attempt < MAX_OCC_RETRIES - 1:
                    await asyncio.sleep(0.05 * (2**attempt))
                    continue
                raise
        raise RuntimeError("unreachable")

    async def _record_node_execution(
        self,
        *,
        node_name: str,
        input_keys: list[str],
        output_keys: list[str],
        duration_ms: int,
        llm_called: bool = False,
        llm_tokens_input: int | None = None,
        llm_tokens_output: int | None = None,
        llm_cost_usd: float | None = None,
    ) -> None:
        assert self.session_id is not None
        assert self.application_id is not None
        self._seq += 1
        self._last_node_name_cache = node_name

        if llm_called and llm_tokens_input is not None:
            self._llm_calls += 1
            self._tokens_used += int(llm_tokens_input + (llm_tokens_output or 0))
            if llm_cost_usd:
                self._cost_usd += float(llm_cost_usd)

        await self._append_session_event(
            AgentNodeExecuted(
                session_id=self.session_id,
                agent_type=self.agent_type,
                node_name=node_name,
                node_sequence=self._seq,
                input_keys=input_keys,
                output_keys=output_keys,
                llm_called=llm_called,
                llm_tokens_input=llm_tokens_input,
                llm_tokens_output=llm_tokens_output,
                llm_cost_usd=llm_cost_usd,
                duration_ms=duration_ms,
                executed_at=datetime.now(tz=None),
            )
        )

    async def _record_tool_call(
        self,
        *,
        tool_name: str,
        tool_input_summary: str,
        tool_output_summary: str,
        tool_duration_ms: int,
    ) -> None:
        assert self.session_id is not None
        await self._append_session_event(
            AgentToolCalled(
                session_id=self.session_id,
                agent_type=self.agent_type,
                tool_name=tool_name,
                tool_input_summary=tool_input_summary,
                tool_output_summary=tool_output_summary,
                tool_duration_ms=tool_duration_ms,
                called_at=datetime.now(tz=None),
            )
        )

    async def _record_output_written(
        self,
        *,
        events_written: list[dict[str, Any]],
        output_summary: str,
    ) -> None:
        assert self.session_id is not None
        assert self.application_id is not None
        await self._append_session_event(
            AgentOutputWritten(
                session_id=self.session_id,
                agent_type=self.agent_type,
                application_id=self.application_id,
                events_written=events_written,
                output_summary=output_summary,
                written_at=datetime.now(tz=None),
            )
        )

    async def _complete_session(self, *, result: dict[str, Any]) -> None:
        assert self.session_id is not None
        assert self.application_id is not None
        total_duration_ms = int((time.time() - self._t0) * 1000)
        await self._append_session_event(
            AgentSessionCompleted(
                session_id=self.session_id,
                agent_type=self.agent_type,
                agent_id=self.agent_id,
                application_id=self.application_id,
                total_nodes_executed=self._seq,
                total_llm_calls=self._llm_calls,
                total_tokens_used=self._tokens_used,
                total_cost_usd=round(self._cost_usd, 6),
                total_duration_ms=total_duration_ms,
                next_agent_triggered=result.get("next_agent_triggered"),
                completed_at=datetime.now(tz=None),
            )
        )

    async def _fail_session(
        self,
        *,
        error_type: str,
        error_message: str,
        last_successful_node: str | None,
        recoverable: bool,
    ) -> None:
        assert self.session_id is not None
        assert self.application_id is not None
        await self._append_session_event(
            AgentSessionFailed(
                session_id=self.session_id,
                agent_type=self.agent_type,
                agent_id=self.agent_id,
                application_id=self.application_id,
                error_type=error_type,
                error_message=error_message[:500],
                last_successful_node=last_successful_node,
                recoverable=recoverable,
                failed_at=datetime.now(tz=None),
            )
        )

    async def _call_llm(self, system: str, user: str, *, max_tokens: int = 800) -> LlmCallResult:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = resp.content[0].text
        input_tokens = int(resp.usage.input_tokens or 0)
        output_tokens = int(resp.usage.output_tokens or 0)

        # Very rough cost model; Week-5 uses Sentinel CostAttributor for final numbers.
        cost_usd = (input_tokens / 1_000_000) * 3.0 + (output_tokens / 1_000_000) * 15.0
        return LlmCallResult(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(float(cost_usd), 6),
        )

    @staticmethod
    def _sha(data: Any) -> str:
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]

