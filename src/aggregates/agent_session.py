"""
AgentSession aggregate.

Tracks all actions taken by a specific AI agent instance during a work session.
Enforces:
  BR-2  Gas Town invariant: AgentContextLoaded must be the first event before
        any decision event can be appended.
  BR-3  Model version locking: once a session is started with a model version,
        no subsequent events may reference a different model version.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.models.events import SessionHealth, StoredEvent
from src.models.exceptions import AgentContextNotLoadedError, DomainError

if TYPE_CHECKING:
    from src.event_store import EventStore


class AgentSessionAggregate:
    """Consistency boundary for an AI agent's work session."""

    def __init__(self, agent_id: str, session_id: str) -> None:
        self.agent_id = agent_id
        self.session_id = session_id
        self.version: int = 0

        # Tracked fields
        self.context_loaded: bool = False
        self.model_version: str | None = None
        self.context_token_count: int = 0
        self.context_source: str = ""
        self.applications_processed: list[str] = []
        self.health: SessionHealth = SessionHealth.OK
        self.last_decision_event_type: str | None = None
        self.last_decision_completed: bool = True

    # ------------------------------------------------------------------
    # Reconstruction
    # ------------------------------------------------------------------

    @classmethod
    async def load(
        cls,
        store: "EventStore",
        agent_id: str,
        session_id: str,
    ) -> "AgentSessionAggregate":
        """Reconstruct aggregate state by replaying the agent session stream."""
        events = await store.load_stream(f"agent-{agent_id}-{session_id}")
        agg = cls(agent_id=agent_id, session_id=session_id)
        for event in events:
            agg._apply(event)
        return agg

    def _apply(self, event: StoredEvent) -> None:
        """Route event to its handler and advance version."""
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_AgentContextLoaded(self, event: StoredEvent) -> None:
        self.context_loaded = True
        self.model_version = event.payload["model_version"]
        self.context_token_count = event.payload.get("context_token_count", 0)
        self.context_source = event.payload.get("context_source", "")

    def _on_CreditAnalysisCompleted(self, event: StoredEvent) -> None:
        app_id = event.payload.get("application_id", "")
        if app_id and app_id not in self.applications_processed:
            self.applications_processed.append(app_id)
        self.last_decision_event_type = "CreditAnalysisCompleted"
        self.last_decision_completed = True

    def _on_FraudScreeningCompleted(self, event: StoredEvent) -> None:
        app_id = event.payload.get("application_id", "")
        if app_id and app_id not in self.applications_processed:
            self.applications_processed.append(app_id)

    def _on_DecisionGenerated(self, event: StoredEvent) -> None:
        self.last_decision_event_type = "DecisionGenerated"
        self.last_decision_completed = True

    # ------------------------------------------------------------------
    # Business rule assertions
    # ------------------------------------------------------------------

    def assert_context_loaded(self) -> None:
        """BR-2: Gas Town invariant — context must be loaded before any decision."""
        if not self.context_loaded:
            raise AgentContextNotLoadedError(
                message="",
                aggregate_type="AgentSession",
                aggregate_id=f"{self.agent_id}/{self.session_id}",
                agent_id=self.agent_id,
                session_id=self.session_id,
            )

    def assert_model_version_current(self, model_version: str) -> None:
        """BR-3: Model version must match the version declared in AgentContextLoaded."""
        if self.model_version is not None and self.model_version != model_version:
            raise DomainError(
                message=(
                    f"Model version mismatch: session started with "
                    f"'{self.model_version}', command uses '{model_version}'."
                ),
                aggregate_type="AgentSession",
                aggregate_id=f"{self.agent_id}/{self.session_id}",
            )

    def assert_processed_application(self, application_id: str) -> None:
        """Assert this session processed the given application (for causal chain)."""
        if application_id not in self.applications_processed:
            raise DomainError(
                message=(
                    f"Session '{self.session_id}' has no decision for "
                    f"application '{application_id}'."
                ),
                aggregate_type="AgentSession",
                aggregate_id=f"{self.agent_id}/{self.session_id}",
            )

    @property
    def stream_id(self) -> str:
        return f"agent-{self.agent_id}-{self.session_id}"
