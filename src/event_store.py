"""
EventStore — The core append-only event log.

Interface is fixed by the challenge specification.
Implementation uses asyncpg for async PostgreSQL access.

Key design decisions (see DESIGN.md §EventStoreDB Comparison):
- Optimistic concurrency enforced via UNIQUE(stream_id, stream_position).
  PostgreSQL constraint violation maps to OptimisticConcurrencyError.
- Outbox written in the SAME transaction as events (atomicity guarantee).
- UpcasterRegistry applied transparently on every load_stream() / load_all() call.
- stream_position starts at 1 (position 0 is reserved for "no events").
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from src.models.events import BaseEvent, StoredEvent, StreamMetadata
from src.models.exceptions import (
    OptimisticConcurrencyError,
    StreamArchivedError,
    StreamNotFoundError,
)

# The UpcasterRegistry is imported lazily via a module-level singleton
# to avoid circular imports (upcasters import events, events don't import upcasters).
_REGISTRY: "UpcasterRegistry | None" = None  # noqa: F821  # type: ignore[name-defined]


def set_registry(registry: object) -> None:
    """Inject the global UpcasterRegistry instance (called during app startup)."""
    global _REGISTRY
    _REGISTRY = registry  # type: ignore[assignment]


def _upcast(event: StoredEvent) -> StoredEvent:
    """Apply registered upcasters if a registry has been set."""
    if _REGISTRY is None:
        return event
    return _REGISTRY.upcast(event)  # type: ignore[attr-defined]


class EventStore:
    """Async PostgreSQL-backed append-only event store.

    Args:
        pool: asyncpg connection pool (provided by create_pool()).
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def append(
        self,
        stream_id: str,
        events: list[BaseEvent],
        expected_version: int,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> int:
        """Atomically append events to a stream.

        Args:
            stream_id: Target stream identifier (e.g. "loan-abc123").
            events: List of domain events to append.
            expected_version: -1 for a new stream; N for an exact version match.
                If the stream's current_version != expected_version, raises
                OptimisticConcurrencyError.
            correlation_id: Propagated correlation identifier (optional).
            causation_id: Immediate cause event identifier (optional).

        Returns:
            New stream version after the append.

        Raises:
            OptimisticConcurrencyError: Version mismatch detected.
            StreamArchivedError: Target stream is archived.
        """
        if not events:
            # Nothing to append — return current version
            return await self.stream_version(stream_id)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # ── 1. Check / create stream ──────────────────────────
                stream_row = await conn.fetchrow(
                    "SELECT current_version, archived_at "
                    "FROM event_streams WHERE stream_id = $1 FOR UPDATE",
                    stream_id,
                )

                if stream_row is None:
                    # New stream
                    if expected_version not in (-1, 0):
                        raise OptimisticConcurrencyError(
                            stream_id=stream_id,
                            expected_version=expected_version,
                            actual_version=0,
                        )
                    # Infer aggregate_type from stream_id prefix
                    aggregate_type = stream_id.split("-")[0]
                    await conn.execute(
                        "INSERT INTO event_streams "
                        "(stream_id, aggregate_type, current_version) "
                        "VALUES ($1, $2, 0)",
                        stream_id,
                        aggregate_type,
                    )
                    current_version = 0
                else:
                    if stream_row["archived_at"] is not None:
                        raise StreamArchivedError(stream_id=stream_id)

                    current_version = stream_row["current_version"]
                    if expected_version not in (-1, current_version):
                        raise OptimisticConcurrencyError(
                            stream_id=stream_id,
                            expected_version=expected_version,
                            actual_version=current_version,
                        )

                # ── 2. Insert events ──────────────────────────────────
                new_version = current_version
                metadata: dict[str, Any] = {}
                if correlation_id:
                    metadata["correlation_id"] = correlation_id
                if causation_id:
                    metadata["causation_id"] = causation_id

                inserted_ids: list[UUID] = []
                for event in events:
                    new_version += 1
                    payload = event.payload_dict()
                    row = await conn.fetchrow(
                        "INSERT INTO events "
                        "(stream_id, stream_position, event_type, event_version, "
                        " payload, metadata) "
                        "VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb) "
                        "RETURNING event_id",
                        stream_id,
                        new_version,
                        event.event_type,
                        event.event_version,
                        json.dumps(payload, default=str),
                        json.dumps(metadata, default=str),
                    )
                    inserted_ids.append(row["event_id"])

                # ── 3. Update stream version ──────────────────────────
                await conn.execute(
                    "UPDATE event_streams SET current_version = $1 "
                    "WHERE stream_id = $2",
                    new_version,
                    stream_id,
                )

                # ── 4. Write to outbox (same transaction) ─────────────
                for event, event_id in zip(events, inserted_ids, strict=True):
                    await conn.execute(
                        "INSERT INTO outbox (event_id, destination, payload) "
                        "VALUES ($1, $2, $3::jsonb)",
                        event_id,
                        "internal",
                        json.dumps(
                            {
                                "event_type": event.event_type,
                                "stream_id": stream_id,
                                **event.payload_dict(),
                            },
                            default=str,
                        ),
                    )

        return new_version

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[StoredEvent]:
        """Load events from a stream, upcasted to their latest version.

        Args:
            stream_id: Target stream.
            from_position: Inclusive lower bound on stream_position (default 0).
            to_position: Inclusive upper bound (default None = all).

        Returns:
            List of StoredEvent in stream order, each upcasted if applicable.
        """
        async with self._pool.acquire() as conn:
            if to_position is not None:
                rows = await conn.fetch(
                    "SELECT event_id, stream_id, stream_position, global_position, "
                    "       event_type, event_version, payload, metadata, recorded_at "
                    "FROM events "
                    "WHERE stream_id = $1 "
                    "  AND stream_position >= $2 "
                    "  AND stream_position <= $3 "
                    "ORDER BY stream_position",
                    stream_id,
                    from_position,
                    to_position,
                )
            else:
                rows = await conn.fetch(
                    "SELECT event_id, stream_id, stream_position, global_position, "
                    "       event_type, event_version, payload, metadata, recorded_at "
                    "FROM events "
                    "WHERE stream_id = $1 AND stream_position >= $2 "
                    "ORDER BY stream_position",
                    stream_id,
                    from_position,
                )
        return [_upcast(_row_to_stored_event(r)) for r in rows]

    async def load_all(
        self,
        from_global_position: int = 0,
        event_types: list[str] | None = None,
        batch_size: int = 500,
    ) -> AsyncIterator[StoredEvent]:
        """Async generator yielding all events from a global position.

        Designed for projection daemon replay — streams in batches to avoid
        loading the full event log into memory.
        """
        current_position = from_global_position
        async with self._pool.acquire() as conn:
            while True:
                if event_types:
                    rows = await conn.fetch(
                        "SELECT event_id, stream_id, stream_position, global_position, "
                        "       event_type, event_version, payload, metadata, recorded_at "
                        "FROM events "
                        "WHERE global_position >= $1 "
                        "  AND event_type = ANY($2) "
                        "ORDER BY global_position "
                        "LIMIT $3",
                        current_position,
                        event_types,
                        batch_size,
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT event_id, stream_id, stream_position, global_position, "
                        "       event_type, event_version, payload, metadata, recorded_at "
                        "FROM events "
                        "WHERE global_position >= $1 "
                        "ORDER BY global_position "
                        "LIMIT $2",
                        current_position,
                        batch_size,
                    )

                if not rows:
                    break

                for row in rows:
                    yield _upcast(_row_to_stored_event(row))

                last = rows[-1]["global_position"]
                current_position = last + 1
                if len(rows) < batch_size:
                    break

    # ------------------------------------------------------------------
    # Stream metadata
    # ------------------------------------------------------------------

    async def stream_version(self, stream_id: str) -> int:
        """Return the current version of a stream (0 if it does not exist)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT current_version FROM event_streams WHERE stream_id = $1",
                stream_id,
            )
        return row["current_version"] if row else 0

    async def archive_stream(self, stream_id: str) -> None:
        """Mark a stream as archived. Archived streams cannot receive new events."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE event_streams "
                "SET archived_at = NOW() "
                "WHERE stream_id = $1 AND archived_at IS NULL",
                stream_id,
            )
        if result == "UPDATE 0":
            raise StreamNotFoundError(stream_id=stream_id)

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata:
        """Return metadata for a stream.

        Raises:
            StreamNotFoundError: If the stream does not exist.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT stream_id, aggregate_type, current_version, "
                "       created_at, archived_at, metadata "
                "FROM event_streams WHERE stream_id = $1",
                stream_id,
            )
        if row is None:
            raise StreamNotFoundError(stream_id=stream_id)
        return StreamMetadata(
            stream_id=row["stream_id"],
            aggregate_type=row["aggregate_type"],
            current_version=row["current_version"],
            created_at=row["created_at"],
            archived_at=row["archived_at"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _row_to_stored_event(row: asyncpg.Record) -> StoredEvent:
    """Convert an asyncpg row to a StoredEvent."""
    payload = row["payload"]
    metadata = row["metadata"]
    return StoredEvent(
        event_id=row["event_id"],
        stream_id=row["stream_id"],
        stream_position=row["stream_position"],
        global_position=row["global_position"],
        event_type=row["event_type"],
        event_version=row["event_version"],
        payload=payload if isinstance(payload, dict) else json.loads(payload),
        metadata=metadata if isinstance(metadata, dict) else json.loads(metadata),
        recorded_at=row["recorded_at"],
    )
