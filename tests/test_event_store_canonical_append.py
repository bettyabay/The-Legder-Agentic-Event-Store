from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from src.event_store import EventStore


@pytest.mark.asyncio
async def test_event_store_append_supports_starter_canonical_events(db_pool) -> None:
    """
    EventStore.append() must accept canonical Week-5 event objects coming from
    starter/ledger/schema/events.py (which expose to_store_dict()).
    """
    # Import Week-5 canonical event classes from starter/ledger.
    # starter/ is not a Python package (no starter/__init__.py), so we add it to sys.path.
    starter_path = Path(__file__).parent.parent / "starter"
    sys.path.insert(0, str(starter_path))

    from ledger.schema.events import ApplicationSubmitted, LoanPurpose  # type: ignore

    store = EventStore(pool=db_pool)
    app_id = f"canon-{uuid4().hex[:8]}"
    stream_id = f"loan-{app_id}"

    event = ApplicationSubmitted(
        application_id=app_id,
        applicant_id="applicant-1",
        requested_amount_usd=Decimal("250000.00"),
        loan_purpose=LoanPurpose.WORKING_CAPITAL,
        loan_term_months=24,
        submission_channel="test",
        contact_email="a@example.com",
        contact_name="Alice",
        submitted_at=datetime.now(tz=timezone.utc),
        application_reference="ref-1",
    )

    new_version = await store.append(
        stream_id=stream_id,
        events=[event],
        expected_version=-1,
    )

    assert new_version == 1

    loaded = await store.load_stream(stream_id)
    assert len(loaded) == 1
    assert loaded[0].event_type == "ApplicationSubmitted"
    assert loaded[0].payload["applicant_id"] == "applicant-1"
    assert "requested_amount_usd" in loaded[0].payload

