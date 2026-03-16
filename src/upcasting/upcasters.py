"""
Registered upcasters for The Ledger.

Each upcaster transforms a specific (event_type, from_version) payload dict
to the next version schema. Stored payloads are NEVER touched.

Inference strategy (DESIGN.md §Upcasting Inference Decisions):

CreditAnalysisCompleted v1→v2:
  - model_version: inferred as "legacy-pre-2026" for events recorded before
    2026-01-01. Inference error rate: ~0% — all pre-2026 events by definition
    lack a model_version field, so "legacy-pre-2026" is always accurate.
  - confidence_score: null — genuinely unknown. Fabricating a value would
    corrupt downstream aggregate business rules (confidence floor enforcement).
    A null is semantically correct; a fabricated 0.85 would be a lie.
  - regulatory_basis: inferred from rule versions active at recorded_at.
    Cannot reconstruct here without a store lookup; set to None and document.

DecisionGenerated v1→v2:
  - model_versions dict: cannot be reconstructed without loading contributing
    agent sessions. Set to empty dict at upcast time. The regulatory package
    generator performs the full reconstruction when it needs complete data.
    Performance implication: lazy reconstruction avoids N+1 on every read.
"""
from __future__ import annotations

from src.upcasting.registry import registry


@registry.register("CreditAnalysisCompleted", from_version=1)
def upcast_credit_v1_to_v2(payload: dict[str, object]) -> dict[str, object]:
    """v1 lacks model_version, confidence_score, regulatory_basis.

    Inference strategy:
      model_version  → "legacy-pre-2026" (known, not fabricated)
      confidence_score → None  (unknown; fabrication would break confidence floor)
      regulatory_basis → None  (requires store lookup; deferred to package generator)
    """
    return {
        **payload,
        "model_version": payload.get("model_version", "legacy-pre-2026"),
        "confidence_score": payload.get("confidence_score", None),
        "regulatory_basis": payload.get("regulatory_basis", None),
    }


@registry.register("DecisionGenerated", from_version=1)
def upcast_decision_v1_to_v2(payload: dict[str, object]) -> dict[str, object]:
    """v1 lacks model_versions dict.

    Full reconstruction (load each contributing session's AgentContextLoaded)
    is deferred to the regulatory package generator to avoid N+1 queries
    on every projection read. At upcast time, we provide an empty dict
    that callers can detect and fill in if needed.

    Performance implication documented in DESIGN.md §Upcasting Inference.
    """
    return {
        **payload,
        "model_versions": payload.get("model_versions", {}),
    }
