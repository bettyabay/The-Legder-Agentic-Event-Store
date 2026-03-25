from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Optional

import asyncpg
import asyncio

from src.outbox.publisher import KafkaOutboxPublisher, LoggingOutboxPublisher, OutboxPublisher

logger = logging.getLogger(__name__)

OUTBOX_DESTINATION_KAFKA = "kafka-week-10"


@dataclass
class OutboxPublishConfig:
    enabled: bool = False
    poll_interval_ms: int = 250
    batch_size: int = 50
    destination: str = OUTBOX_DESTINATION_KAFKA
    kafka_topic: str = "ledger.events"
    max_attempts: int = 10
    dry_run: bool = False


class OutboxPublisherDaemon:
    """Background daemon that drains the `outbox` table and forwards messages.

    By default it's opt-in (env controlled) to avoid surprising external calls.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        publisher: OutboxPublisher,
        config: OutboxPublishConfig,
    ) -> None:
        self._pool = pool
        self._publisher = publisher
        self._cfg = config
        self._running = False
        self._last_processed_at: Optional[datetime] = None
        self._processed_count: int = 0
        self._failed_count: int = 0

    @classmethod
    def from_env(cls, pool: asyncpg.Pool) -> "OutboxPublisherDaemon":
        enabled = os.environ.get("LEDGER_ENABLE_OUTBOX_PUBLISHER", "").lower() in (
            "1",
            "true",
            "yes",
        )
        dry_run = os.environ.get("LEDGER_OUTBOX_DRY_RUN", "").lower() in ("1", "true", "yes")

        config = OutboxPublishConfig(
            enabled=enabled,
            poll_interval_ms=int(os.environ.get("LEDGER_OUTBOX_POLL_MS", "250")),
            batch_size=int(os.environ.get("LEDGER_OUTBOX_BATCH_SIZE", "50")),
            destination=os.environ.get("LEDGER_OUTBOX_DESTINATION", OUTBOX_DESTINATION_KAFKA),
            kafka_topic=os.environ.get("KAFKA_TOPIC", "ledger.events"),
            max_attempts=int(os.environ.get("LEDGER_OUTBOX_MAX_ATTEMPTS", "10")),
            dry_run=dry_run,
        )

        if not enabled:
            # Still create daemon, but the caller should avoid running it.
            publisher: OutboxPublisher = LoggingOutboxPublisher()
            return cls(pool=pool, publisher=publisher, config=config)

        brokers_env = os.environ.get("KAFKA_BROKERS", "").strip()
        if not brokers_env:
            raise RuntimeError("LEDGER_ENABLE_OUTBOX_PUBLISHER is enabled but KAFKA_BROKERS is empty.")

        brokers = [b.strip() for b in brokers_env.split(",") if b.strip()]
        if dry_run:
            publisher = LoggingOutboxPublisher()
        else:
            publisher = KafkaOutboxPublisher(brokers=brokers)
        return cls(pool=pool, publisher=publisher, config=config)

    async def run_forever(self) -> None:
        self._running = True
        while self._running:
            try:
                await self._process_batch()
            except Exception:
                logger.exception("Outbox publisher daemon batch failed")
            await asyncio.sleep(self._cfg.poll_interval_ms / 1000)

    async def stop(self) -> None:
        self._running = False

    async def _process_batch(self) -> None:
        if not self._cfg.enabled:
            return

        async with self._pool.acquire() as conn:
            # Keep transaction short; we lock rows and publish inside the transaction.
            # For production, a more robust in-flight state is recommended.
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    SELECT id, destination, payload, attempts
                    FROM outbox
                    WHERE published_at IS NULL
                      AND destination = $1
                      AND attempts < $2
                    ORDER BY created_at ASC
                    LIMIT $3
                    FOR UPDATE SKIP LOCKED
                    """,
                    self._cfg.destination,
                    self._cfg.max_attempts,
                    self._cfg.batch_size,
                )

                if not rows:
                    return

                now = datetime.now(tz=UTC)
                for r in rows:
                    outbox_id = r["id"]
                    attempts = int(r["attempts"])
                    topic = self._cfg.kafka_topic
                    payload = r["payload"]

                    try:
                        # Payload is already a JSON-decoded dict
                        msg = payload if isinstance(payload, dict) else {"payload": payload}
                        await self._publisher.publish(topic, msg)
                        if self._cfg.dry_run:
                            # In dry-run mode we intentionally do NOT mark messages
                            # as published so that a later real publisher run can
                            # forward the same events.
                            await conn.execute(
                                """
                                UPDATE outbox
                                SET attempts = attempts + 1
                                WHERE id = $1
                                  AND published_at IS NULL
                                """,
                                outbox_id,
                            )
                        else:
                            await conn.execute(
                                """
                                UPDATE outbox
                                SET published_at = $1,
                                    attempts = attempts + 1
                                WHERE id = $2
                                  AND published_at IS NULL
                                """,
                                now,
                                outbox_id,
                            )
                        self._processed_count += 1
                        self._last_processed_at = now
                    except Exception as exc:
                        self._failed_count += 1
                        logger.warning(
                            "Outbox publish failed (id=%s attempt=%s): %s",
                            outbox_id,
                            attempts + 1,
                            exc,
                        )
                        await conn.execute(
                            """
                            UPDATE outbox
                            SET attempts = attempts + 1
                            WHERE id = $1
                              AND published_at IS NULL
                            """,
                            outbox_id,
                        )

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self._cfg.enabled,
            "destination": self._cfg.destination,
            "processed_count": self._processed_count,
            "failed_count": self._failed_count,
            "last_processed_at": self._last_processed_at.isoformat() if self._last_processed_at else None,
        }

