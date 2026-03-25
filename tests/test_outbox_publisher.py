from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

from src.event_store import EventStore
from src.models.events import ApplicationSubmitted
from src.outbox.daemon import OutboxPublishConfig, OutboxPublisherDaemon


class FakePublisherSuccess:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, message: dict[str, Any]) -> None:
        self.published.append((topic, message))


class FakePublisherFailure:
    async def publish(self, topic: str, message: dict[str, Any]) -> None:
        raise RuntimeError("test publish failure")


@pytest.mark.asyncio
async def test_outbox_publisher_marks_published_at(
    db_pool: asyncpg.Pool,
) -> None:
    store = EventStore(pool=db_pool)
    app_id = f"outbox-test-{uuid.uuid4().hex[:8]}"
    stream_id = f"loan-{app_id}"

    now = datetime.now(tz=UTC)
    await store.append(
        stream_id=stream_id,
        events=[
            ApplicationSubmitted(
                application_id=app_id,
                applicant_id="applicant-outbox",
                requested_amount_usd=100_000.0,
                loan_purpose="outbox test",
                submission_channel="test",
                submitted_at=now,
            )
        ],
        expected_version=-1,
    )

    publisher = FakePublisherSuccess()
    cfg = OutboxPublishConfig(enabled=True, batch_size=10, destination="kafka-week-10", kafka_topic="ledger.events")
    daemon = OutboxPublisherDaemon(pool=db_pool, publisher=publisher, config=cfg)

    await daemon._process_batch()

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT published_at, attempts
            FROM outbox
            WHERE payload->>'stream_id' = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            stream_id,
        )

    assert row is not None, "Expected an outbox row to be created"
    assert row["published_at"] is not None, "published_at should be set after successful publish"
    assert int(row["attempts"]) == 1, "attempts should increment to 1 after successful publish"
    assert publisher.published, "Fake publisher should have been called"


@pytest.mark.asyncio
async def test_outbox_publisher_increments_attempts_on_failure(
    db_pool: asyncpg.Pool,
) -> None:
    store = EventStore(pool=db_pool)
    app_id = f"outbox-fail-{uuid.uuid4().hex[:8]}"
    stream_id = f"loan-{app_id}"

    now = datetime.now(tz=UTC)
    await store.append(
        stream_id=stream_id,
        events=[
            ApplicationSubmitted(
                application_id=app_id,
                applicant_id="applicant-outbox",
                requested_amount_usd=100_000.0,
                loan_purpose="outbox fail test",
                submission_channel="test",
                submitted_at=now,
            )
        ],
        expected_version=-1,
    )

    publisher = FakePublisherFailure()
    cfg = OutboxPublishConfig(enabled=True, batch_size=10, destination="kafka-week-10", kafka_topic="ledger.events")
    daemon = OutboxPublisherDaemon(pool=db_pool, publisher=publisher, config=cfg)

    await daemon._process_batch()

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT published_at, attempts
            FROM outbox
            WHERE payload->>'stream_id' = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            stream_id,
        )

    assert row is not None, "Expected an outbox row to be created"
    assert row["published_at"] is None, "published_at should remain NULL after failed publish"
    assert int(row["attempts"]) == 1, "attempts should increment even on failure"

