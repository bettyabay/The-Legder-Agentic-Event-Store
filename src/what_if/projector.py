"""
What-If Projector — Counterfactual scenario analysis.

Allows the Apex compliance team to ask: "What would the decision have been
if we had used the March risk model instead of the February risk model?"

The projector replays application history with a substituted event injected
at the branch point. It NEVER writes counterfactual events to the real store.

Causal dependency:
  An event is causally DEPENDENT on a branched event if its causation_id
  (or correlation_id chain) traces back to an event at or after the branch point.
  Dependent events are skipped in the counterfactual replay.
  Independent events (different causation chain) are included.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.event_store import EventStore
from src.models.events import BaseEvent, StoredEvent

logger = logging.getLogger(__name__)


class Projection(Protocol):
    """Minimal protocol for what-if projection evaluation."""

    name: str

    async def handle(self, event: StoredEvent, conn: Any) -> None: ...


@dataclass
class WhatIfResult:
    """Result of a counterfactual scenario evaluation."""

    application_id: str
    branch_at_event_type: str
    real_outcome: dict[str, Any]
    counterfactual_outcome: dict[str, Any]
    divergence_events: list[dict[str, Any]]
    events_replayed_real: int
    events_replayed_counterfactual: int


async def run_what_if(
    store: EventStore,
    application_id: str,
    branch_at_event_type: str,
    counterfactual_events: list[BaseEvent],
    projections: list[Any] | None = None,
) -> WhatIfResult:
    """Run a what-if scenario by injecting counterfactual events at a branch point.

    Steps:
      1. Load all events for the application stream up to the branch point.
      2. At the branch point, inject counterfactual_events instead of the real ones.
      3. Continue replaying real events that are causally INDEPENDENT of the branch.
      4. Skip real events that are causally DEPENDENT on the branched events.
      5. Evaluate the real and counterfactual streams through the provided projections.
      6. Return WhatIfResult comparing real and counterfactual outcomes.

    CRITICAL: Does NOT write any events to the real store.
    """
    stream_id = f"loan-{application_id}"
    all_events = await store.load_stream(stream_id)

    # The canonical agent pipeline in this repo writes CreditAnalysisCompleted
    # to the *credit* stream (credit-{application_id}), while the what-if
    # projector evaluates the loan stream (loan-{application_id}).
    #
    # If the branch event type is missing on the loan stream, inject the most
    # recent matching event from the credit stream so branching works.
    if (
        branch_at_event_type == "CreditAnalysisCompleted"
        and not any(e.event_type == "CreditAnalysisCompleted" for e in all_events)
    ):
        credit_stream_id = f"credit-{application_id}"
        credit_events = await store.load_stream(credit_stream_id)
        credit_completed = next(
            (e for e in reversed(credit_events) if e.event_type == "CreditAnalysisCompleted"),
            None,
        )
        if credit_completed is not None:
            # Insert after the last CreditAnalysisRequested if present; otherwise append.
            last_req_idx = -1
            for i, e in enumerate(all_events):
                if e.event_type == "CreditAnalysisRequested":
                    last_req_idx = i
            if last_req_idx >= 0:
                all_events = (
                    all_events[: last_req_idx + 1] + [credit_completed] + all_events[last_req_idx + 1 :]
                )
            else:
                all_events = all_events + [credit_completed]

    # 1. Partition events at the branch point
    pre_branch: list[StoredEvent] = []
    real_branch_events: list[StoredEvent] = []
    post_branch: list[StoredEvent] = []

    branched = False
    for event in all_events:
        if not branched and event.event_type == branch_at_event_type:
            branched = True
            real_branch_events.append(event)
        elif not branched:
            pre_branch.append(event)
        else:
            post_branch.append(event)

    # 2. Identify causally dependent post-branch events
    branch_event_ids = {str(e.event_id) for e in real_branch_events}
    dependent, independent = _partition_by_causality(post_branch, branch_event_ids)

    # 3. Build real replay sequence
    real_sequence = pre_branch + real_branch_events + independent

    # 4. Build counterfactual replay sequence
    # Convert counterfactual BaseEvents to StoredEvent-like structures for evaluation
    cf_stored = _as_stored_events(counterfactual_events, real_branch_events)
    counterfactual_sequence = pre_branch + cf_stored + independent

    # 5. Evaluate both sequences through a lightweight state machine
    real_outcome = _evaluate_sequence(real_sequence)
    cf_outcome = _evaluate_sequence(counterfactual_sequence)

    # 6. Find divergence events (events where outcome differs)
    divergence = _find_divergence(real_outcome, cf_outcome)

    logger.info(
        "What-if for application '%s' branching at '%s': "
        "real=%s, counterfactual=%s",
        application_id,
        branch_at_event_type,
        real_outcome.get("final_state"),
        cf_outcome.get("final_state"),
    )

    return WhatIfResult(
        application_id=application_id,
        branch_at_event_type=branch_at_event_type,
        real_outcome=real_outcome,
        counterfactual_outcome=cf_outcome,
        divergence_events=divergence,
        events_replayed_real=len(real_sequence),
        events_replayed_counterfactual=len(counterfactual_sequence),
    )


def _partition_by_causality(
    post_branch: list[StoredEvent],
    branch_event_ids: set[str],
) -> tuple[list[StoredEvent], list[StoredEvent]]:
    """Separate post-branch events into dependent and independent groups."""
    dependent: list[StoredEvent] = []
    independent: list[StoredEvent] = []

    for event in post_branch:
        causation_id = event.metadata.get("causation_id", "")
        if causation_id and causation_id in branch_event_ids:
            dependent.append(event)
        else:
            independent.append(event)

    return dependent, independent


def _as_stored_events(
    base_events: list[BaseEvent],
    originals: list[StoredEvent],
) -> list[StoredEvent]:
    """Wrap BaseEvent objects as StoredEvent for replay (counterfactual only)."""
    import uuid
    from datetime import datetime, timezone

    result: list[StoredEvent] = []
    start_pos = originals[0].stream_position if originals else 999
    for i, ev in enumerate(base_events):
        stored = StoredEvent(
            event_id=uuid.uuid4(),
            stream_id=originals[0].stream_id if originals else "counterfactual",
            stream_position=start_pos + i,
            global_position=-1,  # Not stored
            event_type=ev.event_type,
            event_version=ev.event_version,
            payload=ev.payload_dict(),
            metadata={"counterfactual": True},
            recorded_at=datetime.now(tz=timezone.utc),
        )
        result.append(stored)
    return result


def _evaluate_sequence(events: list[StoredEvent]) -> dict[str, Any]:
    """Evaluate a sequence of events through the LoanApplication state machine.

    Returns a dict describing the outcome: final state, risk tier, decision, etc.
    """
    outcome: dict[str, Any] = {
        "final_state": None,
        "risk_tier": None,
        "recommended_limit_usd": None,
        "decision": None,
        "confidence_score": None,
        "events": [e.event_type for e in events],
    }

    for event in events:
        p = event.payload
        if event.event_type == "ApplicationSubmitted":
            outcome["final_state"] = "SUBMITTED"
        elif event.event_type == "CreditAnalysisCompleted":
            # Payload shape differs across stored data vs counterfactual events:
            # - counterfactual: flat fields (risk_tier, recommended_limit_usd, confidence_score)
            # - stored credit stream: decision nested under `decision{...}`
            decision = p.get("decision") if isinstance(p, dict) else None
            if isinstance(decision, dict):
                outcome["risk_tier"] = p.get("risk_tier") or decision.get("risk_tier")
                outcome["recommended_limit_usd"] = p.get("recommended_limit_usd") or decision.get(
                    "recommended_limit_usd"
                )
                # Stored payload uses `confidence`; the event model uses `confidence_score`.
                outcome["confidence_score"] = p.get("confidence_score") or decision.get("confidence")
            else:
                outcome["risk_tier"] = p.get("risk_tier") if isinstance(p, dict) else None
                outcome["recommended_limit_usd"] = p.get("recommended_limit_usd") if isinstance(p, dict) else None
                outcome["confidence_score"] = p.get("confidence_score") if isinstance(p, dict) else None
            outcome["final_state"] = "ANALYSIS_COMPLETE"
        elif event.event_type == "DecisionGenerated":
            outcome["decision"] = p.get("recommendation")
            outcome["final_state"] = "PENDING_DECISION"
        elif event.event_type == "HumanReviewCompleted":
            final = p.get("final_decision", "")
            outcome["final_state"] = "FINAL_APPROVED" if final in ("APPROVED", "APPROVE") else "FINAL_DECLINED"
        elif event.event_type == "ApplicationApproved":
            outcome["final_state"] = "FINAL_APPROVED"
        elif event.event_type == "ApplicationDeclined":
            outcome["final_state"] = "FINAL_DECLINED"

    return outcome


def _find_divergence(
    real: dict[str, Any],
    counterfactual: dict[str, Any],
) -> list[dict[str, Any]]:
    """Find fields where real and counterfactual outcomes differ."""
    divergence: list[dict[str, Any]] = []
    for key in set(real) | set(counterfactual):
        if key == "events":
            continue
        rv = real.get(key)
        cv = counterfactual.get(key)
        if rv != cv:
            divergence.append({"field": key, "real": rv, "counterfactual": cv})
    return divergence
