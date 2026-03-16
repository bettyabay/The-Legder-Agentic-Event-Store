# Contract: EventStore Interface

**Status**: FROZEN after Phase 1 completion (Constitution Principle III)
**Source**: Challenge document Phase 1, "Core Python Interface"
**File**: `src/event_store.py`

---

## Class Signature

```python
from collections.abc import AsyncIterator
from src.models.events import BaseEvent, StoredEvent, StreamMetadata
from src.models.exceptions import OptimisticConcurrencyError, StreamNotFoundError

class EventStore:
    def __init__(self, pool: asyncpg.Pool, upcaster_registry: "UpcasterRegistry") -> None: ...

    async def append(
        self,
        stream_id: str,
        events: list[BaseEvent],
        expected_version: int,          # -1 = new stream; N = exact version required
        correlation_id: str | None = None,
        causation_id:   str | None = None,
    ) -> int:
        """
        Atomically appends events to stream_id.
        Raises OptimisticConcurrencyError if stream version != expected_version.
        Writes to outbox in same transaction.
        Returns new stream version.
        """

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position:   int | None = None,
    ) -> list[StoredEvent]:
        """
        Returns events in stream order.
        Each event is automatically upcasted via UpcasterRegistry before return.
        Raises StreamNotFoundError if stream_id does not exist.
        """

    async def load_all(
        self,
        from_global_position: int = 0,
        event_types: list[str] | None = None,
        batch_size: int = 500,
    ) -> AsyncIterator[StoredEvent]:
        """
        Async generator; yields StoredEvents in global_position order.
        Each event is automatically upcasted before yield.
        Efficient for full replay — does not load all events into memory.
        """

    async def stream_version(self, stream_id: str) -> int:
        """Returns current_version from event_streams. Raises StreamNotFoundError."""

    async def archive_stream(self, stream_id: str) -> None:
        """Sets archived_at on the stream. Archived streams cannot be appended to."""

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata:
        """Returns StreamMetadata for stream_id. Raises StreamNotFoundError."""
```

---

## Behaviour Contracts

### `append` — Optimistic Concurrency

| Condition | Behaviour |
|-----------|-----------|
| `expected_version == -1` AND stream does not exist | Create stream; assign `stream_position` starting at 1 |
| `expected_version == -1` AND stream exists | Raise `OptimisticConcurrencyError(expected=-1, actual=current_version)` |
| `expected_version == N` AND `current_version == N` | Append events starting at `stream_position = N+1`; update `current_version` |
| `expected_version == N` AND `current_version != N` | Raise `OptimisticConcurrencyError(expected=N, actual=current_version)` |
| Stream is archived | Raise `DomainError(rule="archived_stream", message="Cannot append to archived stream")` |

**Atomicity guarantee**: `events` rows + `outbox` rows are inserted in the
same `asyncpg` transaction. Either both commit or both roll back.

### `load_stream` — Upcasting Transparency

Callers NEVER call upcasters manually. `load_stream` applies the upcaster
chain automatically. The raw stored payload is never returned to callers —
they always receive the latest-version representation.

### `load_all` — Generator Protocol

Callers must `async for event in store.load_all(...)`. The generator fetches
events in `batch_size` pages from PostgreSQL, reducing memory footprint during
full replay. `event_types` filtering uses `WHERE event_type = ANY($1)` on the
indexed `event_type` column.

---

## Error Types

```python
class OptimisticConcurrencyError(Exception):
    stream_id: str
    expected_version: int
    actual_version: int
    suggested_action: str = "reload_stream_and_retry"

class StreamNotFoundError(Exception):
    stream_id: str

class DomainError(Exception):
    rule: str       # machine-readable rule name
    message: str    # human-readable description
```

---

## SLO Commitments

| Operation | p99 Target | Notes |
|-----------|-----------|-------|
| `append` (single event, non-concurrent) | < 10 ms | Local Postgres |
| `load_stream` (< 100 events) | < 50 ms | With upcasting |
| `load_all` (first batch yield) | < 100 ms | Projection daemon replay |
| `stream_version` | < 5 ms | Single row lookup |
