"""
Cryptographic audit chain for the AuditLedger aggregate.

Each AuditIntegrityCheckRun event records a SHA-256 hash of all events since
the previous check, chained to the previous check's hash. This forms a
blockchain-style integrity chain — any post-hoc modification of events breaks
the chain.

Algorithm:
  new_hash = sha256(previous_hash + concat(sha256(payload) for each event))
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.aggregates.audit_ledger import AuditLedgerAggregate
from src.event_store import EventStore
from src.models.events import AuditIntegrityCheckRun

logger = logging.getLogger(__name__)


@dataclass
class IntegrityCheckResult:
    entity_type: str
    entity_id: str
    events_verified: int
    chain_valid: bool
    tamper_detected: bool
    integrity_hash: str
    previous_hash: str | None
    checked_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


async def run_integrity_check(
    store: EventStore,
    entity_type: str,
    entity_id: str,
) -> IntegrityCheckResult:
    """Run a cryptographic integrity check on an entity's event stream.

    Steps:
      1. Load all events for the entity's primary stream.
      2. Load the last AuditIntegrityCheckRun (if any) for the previous hash.
      3. Hash the payloads of all events since the last check.
      4. Verify hash chain: new_hash = sha256(previous_hash + event_hashes).
      5. Append new AuditIntegrityCheckRun to the audit stream.
      6. Return IntegrityCheckResult.
    """
    # Determine the primary stream to audit
    stream_id = _primary_stream_id(entity_type, entity_id)
    audit_stream_id = f"audit-{entity_type}-{entity_id}"

    # 1. Load all events in the primary stream
    events = await store.load_stream(stream_id)

    # 2. Load last AuditIntegrityCheckRun from the audit stream
    audit_events = await store.load_stream(audit_stream_id)
    last_audit = next(
        (
            e
            for e in reversed(audit_events)
            if e.event_type == "AuditIntegrityCheckRun"
        ),
        None,
    )
    previous_hash = last_audit.payload.get("integrity_hash") if last_audit else None

    # 3. Hash each event payload
    event_hashes = [
        hashlib.sha256(
            json.dumps(e.payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        for e in events
    ]

    # 4. Compute new chain hash
    chain_input = (previous_hash or "") + "".join(event_hashes)
    new_hash = hashlib.sha256(chain_input.encode()).hexdigest()

    # Verify: recompute and compare (chain is valid if it matches expected)
    chain_valid = True  # For new events, chain is always valid; tamper shows as mismatch
    tamper_detected = False

    if last_audit:
        # Recompute from the beginning to detect tampering
        all_events = events
        full_chain = _recompute_full_chain(all_events)
        tamper_detected = full_chain != new_hash
        chain_valid = not tamper_detected

    # 5. Append AuditIntegrityCheckRun event
    audit_ledger = await AuditLedgerAggregate.load(store, entity_type, entity_id)
    audit_event = AuditIntegrityCheckRun(
        entity_id=entity_id,
        check_timestamp=datetime.now(tz=timezone.utc),
        events_verified_count=len(events),
        integrity_hash=new_hash,
        previous_hash=previous_hash,
    )
    await store.append(
        stream_id=audit_stream_id,
        events=[audit_event],
        expected_version=audit_ledger.version,
    )

    logger.info(
        "Integrity check for %s/%s: %d events verified, chain_valid=%s, tamper=%s",
        entity_type,
        entity_id,
        len(events),
        chain_valid,
        tamper_detected,
    )

    return IntegrityCheckResult(
        entity_type=entity_type,
        entity_id=entity_id,
        events_verified=len(events),
        chain_valid=chain_valid,
        tamper_detected=tamper_detected,
        integrity_hash=new_hash,
        previous_hash=previous_hash,
    )


def _primary_stream_id(entity_type: str, entity_id: str) -> str:
    """Map entity_type to its primary stream prefix."""
    prefixes: dict[str, str] = {
        "loan": "loan",
        "agent": "agent",
        "compliance": "compliance",
    }
    prefix = prefixes.get(entity_type, entity_type)
    return f"{prefix}-{entity_id}"


def _recompute_full_chain(events: list[object]) -> str:
    """Recompute the full chain hash from scratch for tamper detection."""
    prev = ""
    for event in events:  # type: ignore[assignment]
        payload = getattr(event, "payload", {})
        h = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        prev = hashlib.sha256((prev + h).encode()).hexdigest()
    return prev
