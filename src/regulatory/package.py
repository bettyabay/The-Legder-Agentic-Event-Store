"""
Regulatory Examination Package Generator.

Produces a complete, self-contained JSON package that a regulator can verify
against the database independently — they should not need to trust the system
to validate that the package is accurate.

Package contents:
  1. Complete event stream for the application, in order, with full payloads.
  2. State of every projection as it existed at examination_date.
  3. Audit chain integrity verification result.
  4. Human-readable narrative of the application lifecycle.
  5. Model versions, confidence scores, and input data hashes for every AI agent.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.event_store import EventStore
from src.integrity.audit_chain import run_integrity_check
from src.projections.compliance_audit import ComplianceAuditViewProjection

logger = logging.getLogger(__name__)


@dataclass
class RegulatoryPackage:
    application_id: str
    examination_date: str
    generated_at: str
    event_stream: list[dict[str, Any]]
    projection_state_at_examination: dict[str, Any]
    integrity_check: dict[str, Any]
    narrative: str
    agent_participation: list[dict[str, Any]]
    package_hash: str = field(default="")

    def to_json(self) -> str:
        data = {
            "application_id": self.application_id,
            "examination_date": self.examination_date,
            "generated_at": self.generated_at,
            "event_stream": self.event_stream,
            "projection_state_at_examination": self.projection_state_at_examination,
            "integrity_check": self.integrity_check,
            "narrative": self.narrative,
            "agent_participation": self.agent_participation,
            "package_hash": self.package_hash,
        }
        return json.dumps(data, indent=2, default=str)


async def generate_regulatory_package(
    store: EventStore,
    compliance_proj: ComplianceAuditViewProjection,
    application_id: str,
    examination_date: datetime,
) -> RegulatoryPackage:
    """Generate a complete regulatory examination package.

    The package is independently verifiable: the examiner can recompute
    the package_hash from the event_stream to validate integrity.

    Args:
        store: EventStore instance.
        compliance_proj: ComplianceAuditViewProjection for temporal queries.
        application_id: Target application.
        examination_date: Point-in-time for projection snapshots.
    """
    # 1. Load complete event stream
    events = await store.load_stream(f"loan-{application_id}")
    event_dicts: list[dict[str, Any]] = [
        {
            "event_id": str(e.event_id),
            "stream_position": e.stream_position,
            "global_position": e.global_position,
            "event_type": e.event_type,
            "event_version": e.event_version,
            "payload": e.payload,
            "metadata": e.metadata,
            "recorded_at": e.recorded_at.isoformat(),
        }
        for e in events
        if e.recorded_at <= examination_date
    ]

    # 2. Projection state at examination_date
    compliance_at = await compliance_proj.get_compliance_at(application_id, examination_date)
    projection_state = {
        "compliance_audit_view": compliance_at,
        "examination_date": examination_date.isoformat(),
    }

    # 3. Audit chain integrity
    try:
        integrity = await run_integrity_check(store, "loan", application_id)
        integrity_dict: dict[str, Any] = {
            "events_verified": integrity.events_verified,
            "chain_valid": integrity.chain_valid,
            "tamper_detected": integrity.tamper_detected,
            "integrity_hash": integrity.integrity_hash,
        }
    except Exception as exc:
        logger.warning("Integrity check failed for regulatory package: %s", exc)
        integrity_dict = {"error": str(exc), "chain_valid": False}

    # 4. Human-readable narrative
    narrative = _build_narrative(events, examination_date)

    # 5. Agent participation metadata
    agent_participation = _extract_agent_participation(events, examination_date)

    # 6. Compute package hash for independent verification
    package_content = json.dumps(
        {
            "event_stream": event_dicts,
            "integrity_hash": integrity_dict.get("integrity_hash", ""),
        },
        sort_keys=True,
        default=str,
    )
    package_hash = hashlib.sha256(package_content.encode()).hexdigest()

    return RegulatoryPackage(
        application_id=application_id,
        examination_date=examination_date.isoformat(),
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        event_stream=event_dicts,
        projection_state_at_examination=projection_state,
        integrity_check=integrity_dict,
        narrative=narrative,
        agent_participation=agent_participation,
        package_hash=package_hash,
    )


def _build_narrative(
    events: list[Any],
    examination_date: datetime,
) -> str:
    """Produce a plain-English narrative — one sentence per significant event."""
    lines: list[str] = ["Application lifecycle narrative:"]

    for event in events:
        if event.recorded_at > examination_date:
            break
        p = event.payload
        ts = event.recorded_at.strftime("%Y-%m-%d %H:%M:%S UTC")

        if event.event_type == "ApplicationSubmitted":
            lines.append(
                f"  [{ts}] Application submitted by applicant '{p.get('applicant_id')}' "
                f"requesting ${p.get('requested_amount_usd', 0):,.0f} for '{p.get('loan_purpose')}'."
            )
        elif event.event_type == "CreditAnalysisCompleted":
            lines.append(
                f"  [{ts}] Credit analysis completed by agent '{p.get('agent_id')}' "
                f"(model {p.get('model_version', 'unknown')}): "
                f"risk tier '{p.get('risk_tier')}', "
                f"confidence {p.get('confidence_score', 'N/A')}, "
                f"recommended limit ${p.get('recommended_limit_usd', 0):,.0f}."
            )
        elif event.event_type == "FraudScreeningCompleted":
            lines.append(
                f"  [{ts}] Fraud screening completed: score {p.get('fraud_score', 0):.2f}, "
                f"flags: {p.get('anomaly_flags', [])}."
            )
        elif event.event_type == "ComplianceClearanceIssued":
            lines.append(
                f"  [{ts}] Compliance clearance issued under regulation set "
                f"'{p.get('regulation_set_version')}' — all checks passed."
            )
        elif event.event_type == "DecisionGenerated":
            lines.append(
                f"  [{ts}] Decision generated by orchestrator '{p.get('orchestrator_agent_id')}': "
                f"recommendation '{p.get('recommendation')}' "
                f"(confidence {p.get('confidence_score', 0):.2f})."
            )
        elif event.event_type == "HumanReviewCompleted":
            override_note = (
                f" OVERRIDE: {p.get('override_reason')}" if p.get("override") else ""
            )
            lines.append(
                f"  [{ts}] Human review by '{p.get('reviewer_id')}': "
                f"final decision '{p.get('final_decision')}'.{override_note}"
            )
        elif event.event_type in ("ApplicationApproved", "ApplicationDeclined"):
            lines.append(
                f"  [{ts}] Application {event.event_type.replace('Application', '').lower()}."
            )

    return "\n".join(lines)


def _extract_agent_participation(
    events: list[Any],
    examination_date: datetime,
) -> list[dict[str, Any]]:
    """Extract model versions, confidence scores, and input hashes per agent."""
    agents: dict[str, dict[str, Any]] = {}

    for event in events:
        if event.recorded_at > examination_date:
            break
        p = event.payload

        agent_id = p.get("agent_id") or p.get("orchestrator_agent_id")
        if not agent_id:
            continue

        if agent_id not in agents:
            agents[agent_id] = {
                "agent_id": agent_id,
                "model_version": p.get("model_version"),
                "confidence_score": p.get("confidence_score"),
                "input_data_hash": p.get("input_data_hash"),
                "events_contributed": [],
            }

        agents[agent_id]["events_contributed"].append(event.event_type)
        if p.get("model_version"):
            agents[agent_id]["model_version"] = p["model_version"]
        if p.get("confidence_score") is not None:
            agents[agent_id]["confidence_score"] = p["confidence_score"]

    return list(agents.values())
