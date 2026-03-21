"""
Gas Town Agent Memory Pattern — reconstruct_agent_context()

Prevents catastrophic context loss on agent process restart. An agent that
crashes mid-session replays its event stream to reconstruct full context.

Design:
  - Last 3 events are preserved verbatim (high fidelity for recent state).
  - Older events are summarised into prose (token-efficient).
  - PENDING or ERROR events are always preserved verbatim.
  - If the last event was a partial decision (no completion event), the
    context is flagged as NEEDS_RECONCILIATION.

token_budget is a soft limit on context_text length (in approximate tokens).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.event_store import EventStore
from src.models.events import SessionHealth, StoredEvent
from src.models.exceptions import NeedsReconciliationError

logger = logging.getLogger(__name__)

# Events that indicate a "start" of a decision (must be paired with a completion)
_PARTIAL_DECISION_EVENTS = frozenset({
    "CreditAnalysisRequested",
    "ComplianceCheckRequested",
})

# Completion events that close a decision (partner to _PARTIAL_DECISION_EVENTS)
_COMPLETION_EVENTS = frozenset({
    "CreditAnalysisCompleted",
    "FraudScreeningCompleted",
    "ComplianceClearanceIssued",
    "DecisionGenerated",
    "HumanReviewCompleted",
    "ApplicationApproved",
    "ApplicationDeclined",
})

# Events always preserved verbatim regardless of position
_ALWAYS_VERBATIM = frozenset({
    "AgentContextLoaded",
})

_VERBATIM_TAIL = 3  # Last N events are always verbatim


@dataclass
class AgentContext:
    agent_id: str
    session_id: str
    context_text: str
    last_event_position: int
    pending_work: list[str]
    session_health_status: SessionHealth
    model_version: str = ""
    context_source: str = ""
    last_successful_node: str | None = None
    executed_nodes: list[str] = field(default_factory=list)
    reconstructed_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )


async def reconstruct_agent_context(
    store: EventStore,
    agent_id: str,
    session_id: str,
    token_budget: int = 8000,
) -> AgentContext:
    """Reconstruct agent context from the event stream.

    Args:
        store: EventStore instance.
        agent_id: Target agent identifier.
        session_id: Target session identifier.
        token_budget: Approximate max tokens for the context_text summary.

    Returns:
        AgentContext with reconstructed state.

    Note:
        If the last event was a partial decision with no completion event,
        the returned context has session_health_status = NEEDS_RECONCILIATION.
        The agent MUST resolve this before proceeding.
    """
    # Reduced Phase 2/3 uses stream_id = agent-{agent_id}-{session_id}.
    # Week-5 canonical uses agent-{agent_type}-{session_id} and includes session_id
    # inside payload. We try both.
    stream_id = f"agent-{agent_id}-{session_id}"
    events = await store.load_stream(stream_id)
    if not events:
        alt_stream_id = await store.find_session_stream_id(session_id)
        if alt_stream_id:
            stream_id = alt_stream_id
            events = await store.load_stream(stream_id)

    if not events:
        return AgentContext(
            agent_id=agent_id,
            session_id=session_id,
            context_text="No events found for this agent session.",
            last_event_position=0,
            pending_work=[],
            session_health_status=SessionHealth.OK,
        )

    last_event = events[-1]

    # Determine health status
    health = _determine_health(events)

    # Identify pending work
    pending_work = _identify_pending_work(events)

    # Partition events: verbatim tail + summary prefix
    verbatim_events = events[-_VERBATIM_TAIL:]
    summary_events = events[:-_VERBATIM_TAIL] if len(events) > _VERBATIM_TAIL else []

    # Build context text within token budget (approximation: 1 token ≈ 4 chars)
    char_budget = token_budget * 4
    summary_text = _summarise_events(summary_events, char_budget=char_budget - 500)
    verbatim_text = _format_verbatim(verbatim_events)

    context_text = f"{summary_text}\n\n--- RECENT EVENTS (VERBATIM) ---\n{verbatim_text}"
    if len(context_text) > char_budget:
        context_text = context_text[-char_budget:]

    # Extract model version and context source from first event
    model_version = ""
    context_source = ""
    if events:
        if events[0].event_type == "AgentContextLoaded":
            model_version = events[0].payload.get("model_version", "")  # type: ignore[assignment]
            context_source = events[0].payload.get("context_source", "")  # type: ignore[assignment]
        elif events[0].event_type == "AgentSessionStarted":
            model_version = events[0].payload.get("model_version", "")  # type: ignore[assignment]
            context_source = events[0].payload.get("context_source", "")  # type: ignore[assignment]

    # Week-5 canonical node tracking
    last_successful_node: str | None = None
    executed_nodes: list[str] = []
    for ev in events:
        if ev.event_type == "AgentNodeExecuted":
            node_name = ev.payload.get("node_name")
            if isinstance(node_name, str):
                executed_nodes.append(node_name)
                last_successful_node = node_name

    return AgentContext(
        agent_id=agent_id,
        session_id=session_id,
        context_text=context_text,
        last_event_position=last_event.stream_position,
        pending_work=pending_work,
        session_health_status=health,
        model_version=str(model_version),
        context_source=str(context_source),
        last_successful_node=last_successful_node,
        executed_nodes=executed_nodes,
    )


def _determine_health(events: list[StoredEvent]) -> SessionHealth:
    """Determine session health from the event log."""
    if not events:
        return SessionHealth.OK

    last = events[-1]
    if last.event_type in _PARTIAL_DECISION_EVENTS:
        return SessionHealth.NEEDS_RECONCILIATION

    # Week-5 canonical recovery: a failed session indicates it must be reconciled.
    if last.event_type in {"AgentSessionFailed", "AgentSessionRecovered"}:
        return SessionHealth.NEEDS_RECONCILIATION

    return SessionHealth.OK


def _identify_pending_work(events: list[StoredEvent]) -> list[str]:
    """Identify work items that were started but not completed."""
    started: set[str] = set()
    completed_apps: set[str] = set()

    for event in events:
        app_id = event.payload.get("application_id", "")
        if event.event_type in _PARTIAL_DECISION_EVENTS and app_id:
            started.add(str(app_id))
        elif event.event_type in _COMPLETION_EVENTS and app_id:
            completed_apps.add(str(app_id))

    return list(started - completed_apps)


def _summarise_events(
    events: list[StoredEvent],
    char_budget: int = 6000,
) -> str:
    """Convert older events into a token-efficient prose summary."""
    if not events:
        return "(No historical events to summarise.)"

    lines: list[str] = [f"Session history: {len(events)} earlier events."]
    for event in events[-20:]:  # summarise last 20 of the "old" section
        app_id = event.payload.get("application_id", "")
        line = (
            f"  [{event.stream_position}] {event.event_type}"
            f"{' for ' + str(app_id) if app_id else ''}"
            f" at {event.recorded_at.isoformat()[:19]}"
        )
        lines.append(line)

    summary = "\n".join(lines)
    if len(summary) > char_budget:
        summary = summary[-char_budget:]
    return summary


def _format_verbatim(events: list[StoredEvent]) -> str:
    """Format the verbatim tail events as structured text."""
    if not events:
        return "(No recent events.)"

    lines: list[str] = []
    for event in events:
        lines.append(
            f"[pos={event.stream_position}] {event.event_type} "
            f"| recorded_at={event.recorded_at.isoformat()[:19]} "
            f"| payload={event.payload}"
        )
    return "\n".join(lines)
