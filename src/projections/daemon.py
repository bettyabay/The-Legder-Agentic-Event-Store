"""
ProjectionDaemon — Async background processor for projections.

Continuously polls the events table from the last processed global_position,
routes new events to registered projections, and updates checkpoints.

Fault tolerance:
  - If a projection handler raises, the event is retried up to MAX_RETRIES (3).
  - After MAX_RETRIES failures the event is skipped and logged to projection_errors.
  - A daemon that crashes on a bad event is a production incident — we don't crash.

SLO enforcement:
  - get_lag() returns milliseconds between latest event and last processed.
  - CRITICAL log emitted when lag > 3× SLO for 5 consecutive cycles.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import asyncpg

from src.event_store import EventStore
from src.models.events import StoredEvent

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
EVENTS_PER_PROCESS_BATCH = 1000


class Projection(Protocol):
    """Protocol that every projection must implement."""

    name: str
    slo_ms: int

    async def handle(self, event: StoredEvent, conn: asyncpg.Connection) -> None:  # type: ignore[type-arg]
        """Process a single event; must be idempotent."""
        ...

    async def rebuild(self, conn: asyncpg.Connection) -> None:  # type: ignore[type-arg]
        """Truncate projection table and mark for full replay."""
        ...


@dataclass
class LagRecord:
    last_processed_global_position: int = 0
    last_processed_at: datetime = field(
        default_factory=lambda: datetime.now(tz=UTC)
    )
    slo_breach_cycles: int = 0


class ProjectionDaemon:
    """Background asyncio task that drives projections forward.

    Args:
        store: EventStore instance.
        projections: List of Projection implementations to manage.
        pool: asyncpg pool for checkpoint and error table access.
    """

    def __init__(
        self,
        store: EventStore,
        projections: list[Any],
        pool: asyncpg.Pool,
    ) -> None:
        self._store = store
        self._projections: dict[str, Any] = {p.name: p for p in projections}
        self._pool = pool
        self._running = False
        self._lag_records: dict[str, LagRecord] = {
            p.name: LagRecord() for p in projections
        }

    async def run_forever(self, poll_interval_ms: int = 100) -> None:
        """Main loop — polls and processes until stop() is called."""
        self._running = True
        while self._running:
            try:
                await self._process_batch()
            except Exception:
                logger.exception("Unexpected error in projection daemon batch")
            await asyncio.sleep(poll_interval_ms / 1000)

    async def stop(self) -> None:
        """Gracefully stop the daemon."""
        self._running = False

    async def _process_batch(self) -> None:
        """Load events from lowest checkpoint, route to projections, update checkpoints."""
        checkpoints = await self._load_checkpoints()
        # IMPORTANT:
        # If a projection checkpoint row is missing (e.g. after rebuild_projection()
        # deletes its checkpoint row), we must treat that projection as having
        # checkpoint=0. Otherwise min_position could advance past events that
        # that projection still needs to rebuild, leaving it empty.
        # Projection checkpoints record the last processed global_position.
        # When we start the next batch with `>= last_position` we re-process the
        # same event again, burning iterations and slowing catch-up.
        #
        # Treat checkpoint as "last processed" and start from next position:
        # next_pos = last_position + 1, except when last_position is 0
        # (meaning "nothing processed yet").
        proj_next_positions: dict[str, int] = {}
        for projection_name in self._projections:
            last_pos = checkpoints.get(projection_name, 0)
            proj_next_positions[projection_name] = last_pos if last_pos == 0 else last_pos + 1
        min_position = min(proj_next_positions.values()) if proj_next_positions else 0

        events: list[StoredEvent] = []
        async for event in self._store.load_all(
            from_global_position=min_position,
            batch_size=EVENTS_PER_PROCESS_BATCH,
        ):
            events.append(event)
            if len(events) >= EVENTS_PER_PROCESS_BATCH:
                break

        if not events:
            return

        async with self._pool.acquire() as conn:
            for projection_name, projection in self._projections.items():
                proj_next = proj_next_positions.get(projection_name, 0)
                relevant = [e for e in events if e.global_position >= proj_next]
                if not relevant:
                    continue

                for event in relevant:
                    success = await self._handle_with_retry(
                        projection, event, conn, projection_name
                    )
                    if success:
                        await self._update_checkpoint(
                            conn, projection_name, event.global_position
                        )
                        self._lag_records[projection_name].last_processed_global_position = (
                            event.global_position
                        )
                        self._lag_records[projection_name].last_processed_at = (
                            datetime.now(tz=UTC)
                        )

        # Emit CRITICAL logs if SLO is breached repeatedly
        await self._check_slo_breaches()

    async def _handle_with_retry(
        self,
        projection: Any,
        event: StoredEvent,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
        projection_name: str,
    ) -> bool:
        """Attempt to process an event up to MAX_RETRIES times.

        Returns True on success, False after exhausting retries.
        """
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                await projection.handle(event, conn)
                return True
            except Exception as exc:
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "Projection '%s' failed on event %s (attempt %d/%d): %s",
                        projection_name,
                        event.global_position,
                        attempt,
                        MAX_RETRIES,
                        exc,
                    )
                    await asyncio.sleep(0.1 * attempt)
                else:
                    logger.error(
                        "Projection '%s' exhausted retries for event %s (%s). Skipping.",
                        projection_name,
                        event.global_position,
                        event.event_type,
                    )
                    await self._log_projection_error(
                        conn, projection_name, event, str(exc), MAX_RETRIES
                    )
        return False

    async def _load_checkpoints(self) -> dict[str, int]:
        """Load all projection checkpoints from the database."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT projection_name, last_position FROM projection_checkpoints"
            )
        return {row["projection_name"]: row["last_position"] for row in rows}

    async def _update_checkpoint(
        self,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
        projection_name: str,
        position: int,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO projection_checkpoints (projection_name, last_position, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (projection_name)
            DO UPDATE SET last_position = EXCLUDED.last_position, updated_at = NOW()
            """,
            projection_name,
            position,
        )

    async def _log_projection_error(
        self,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
        projection_name: str,
        event: StoredEvent,
        error_message: str,
        retry_count: int,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO projection_errors
              (projection_name, global_position, event_type, error_message, retry_count)
            VALUES ($1, $2, $3, $4, $5)
            """,
            projection_name,
            event.global_position,
            event.event_type,
            error_message,
            retry_count,
        )

    async def _check_slo_breaches(self) -> None:
        """Emit CRITICAL log when lag > 3× SLO for 5 consecutive cycles."""
        for name, projection in self._projections.items():
            lag_ms = await self.get_lag(name)
            slo_ms: int = getattr(projection, "slo_ms", 2000)
            record = self._lag_records[name]
            if lag_ms > 3 * slo_ms:
                record.slo_breach_cycles += 1
                if record.slo_breach_cycles >= 5:
                    logger.critical(
                        "PROJECTION SLO BREACH: '%s' lag=%dms > 3×SLO=%dms "
                        "for %d consecutive cycles.",
                        name,
                        lag_ms,
                        slo_ms,
                        record.slo_breach_cycles,
                    )
            else:
                record.slo_breach_cycles = 0

    async def get_lag(self, projection_name: str) -> int:
        """Return lag in milliseconds for a projection.

        Lag = time since last processed event was recorded_at.
        Returns 0 if no events have been processed yet.
        """
        record = self._lag_records.get(projection_name)
        if record is None:
            return 0
        now = datetime.now(tz=UTC)
        delta = now - record.last_processed_at
        return int(delta.total_seconds() * 1000)

    async def get_all_lags(self) -> dict[str, int]:
        """Return lag in milliseconds for all managed projections."""
        return {name: await self.get_lag(name) for name in self._projections}

    async def rebuild_projection(self, projection_name: str) -> None:
        """Trigger a rebuild of a single projection from scratch.

        Does not pause live reads — projection table is live during rebuild.
        Checkpoint is reset to 0; next batch will replay from the beginning.
        """
        projection = self._projections.get(projection_name)
        if projection is None:
            raise KeyError(f"Unknown projection: {projection_name}")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await projection.rebuild(conn)
                await conn.execute(
                    "DELETE FROM projection_checkpoints WHERE projection_name = $1",
                    projection_name,
                )

        logger.info("Projection '%s' rebuilt and checkpoint reset.", projection_name)
