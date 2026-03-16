# Phase 0 Research: The Ledger — Agentic Event Store

**Branch**: `001-ledger-event-store` | **Date**: 2026-03-16
**Input**: Technical Context from `plan.md`; user-supplied stack constraints

---

## Skipped Clarifications — Planning Decisions

The following questions from the `/speckit.clarify` session were deferred when
the user moved directly to `/speckit.plan`. Answers are encoded here as binding
planning decisions and propagated into the implementation contracts.

| Deferred Question | Planning Decision | Rationale |
|-------------------|-------------------|-----------|
| Q2 — Retry budget for OptimisticConcurrencyError | **Max 3 retries, exponential backoff (100 ms → 200 ms → 400 ms) at the MCP tool / command handler layer; event store surfaces error immediately on every collision** | Challenge DESIGN.md explicitly asks for retry strategy and error rate estimate; 3 retries matches Marten's default retry budget; exponential backoff reduces thundering herd under peak load (100 apps × 4 agents) |
| Q3 — Outbox destination scope | **Internal-only this week: `destination` field records intent (e.g., `"projection-daemon"`, `"kafka-week-10"`); no external bus consumer is implemented; outbox table is written atomically but not polled** | Single-database architecture; Kafka/Redis integration deferred to Week 10 per challenge stack table |
| Q4 — Daemon default retry count and lag SLO breach policy | **Default 3 retries per event per projection; if any projection exceeds its SLO lag by 3× (ApplicationSummary > 1,500 ms, ComplianceAuditView > 6,000 ms) for 5 consecutive poll cycles, daemon logs CRITICAL and continues; no automatic restart** | Fault-tolerant daemon must not crash or self-restart unpredictably; CRITICAL log is the alerting signal for operators |
| Q5 — ComplianceAuditView snapshot trigger strategy | **Event-count trigger: snapshot taken every 100 events per application in the compliance stream; stored as JSONB in a `compliance_snapshots` table alongside position; temporal query scans from the nearest preceding snapshot** | Event-count is deterministic and proportional to data growth; avoids snapshot storm on time-based triggers under variable write load; aligns with Marten's default snapshot strategy |

---

## R-001 — Async PostgreSQL Driver: asyncpg vs psycopg3

**Decision**: `asyncpg` as primary async driver.

**Rationale**:
- asyncpg has the highest raw throughput for bulk event appends; benchmarks show
  2–3× higher write throughput than psycopg3 for the append-heavy event store workload
- asyncpg uses a binary wire protocol, reducing serialisation overhead on JSONB columns
- asyncpg's `Connection.transaction()` context manager is idiomatic for the
  atomic `append + outbox` operation required by FR-010
- psycopg3 async is an acceptable fallback; the interface is identical for this use case

**Implementation note**: All database interactions MUST use `asyncpg.Pool` for
connection pooling. Never use `asyncio.create_task` with raw connections — always
acquire from pool.

**Alternatives considered**:
- `psycopg3` async: compatible; slightly lower write throughput; acceptable fallback
- `SQLAlchemy async`: rejected — ORM abstraction over event tables is forbidden by
  Principle VII of the constitution

---

## R-002 — Optimistic Concurrency Implementation in PostgreSQL

**Decision**: Advisory-lock-free approach using the `UNIQUE (stream_id, stream_position)` constraint combined with a stream version check inside a single transaction.

**Rationale**:
The challenge schema includes `CONSTRAINT uq_stream_position UNIQUE (stream_id, stream_position)`.
The correct concurrency implementation:

```sql
-- Step 1: Fetch current version (inside transaction)
SELECT current_version FROM event_streams
WHERE stream_id = $1
FOR UPDATE;                          -- row-level lock on this stream only

-- Step 2: Check expected version
-- If current_version != expected_version → raise OptimisticConcurrencyError

-- Step 3: Insert events (positions = current_version + 1, +2, ...)
INSERT INTO events (stream_id, stream_position, ...)
VALUES ($1, $current_version + 1, ...);

-- Step 4: Update stream version
UPDATE event_streams SET current_version = $new_version WHERE stream_id = $1;
```

The `SELECT ... FOR UPDATE` on the `event_streams` row serialises concurrent
appends to the same stream without locking the entire `events` table. Two agents
appending to different streams do not block each other.

**Why NOT advisory locks**: Advisory locks require separate lock acquisition,
are not automatically released on connection pool recycle, and add complexity
without benefit given the `FOR UPDATE` approach is sufficient.

**Why NOT application-layer optimistic locking without DB lock**: Pure
compare-and-set at the application layer would require a retry loop within the
transaction, reintroducing the race condition on the `UPDATE`.

**OptimisticConcurrencyError trigger**: If `current_version != expected_version`
after the `SELECT FOR UPDATE`, the transaction is rolled back and
`OptimisticConcurrencyError` is raised immediately with:
```python
OptimisticConcurrencyError(
    stream_id=stream_id,
    expected_version=expected_version,
    actual_version=current_version,
)
```

**Retry strategy** (MCP tool layer, not event store layer):
- Maximum 3 retries
- Backoff: 100 ms, 200 ms, 400 ms (exponential)
- On 4th failure: raise `ConcurrencyExhaustedError` with full context

---

## R-003 — CQRS Projection Pattern: asyncio Background Task

**Decision**: `ProjectionDaemon` runs as a persistent `asyncio.Task` inside the
same process as the MCP server, polling `events` table by `global_position`.

**Rationale**:
- Avoids inter-process coordination (no Redis, no Celery) for the single-database
  architecture mandated by the challenge
- `asyncio.Task` lifecycle aligns naturally with the MCP server's lifespan
- Checkpoint management via `projection_checkpoints` table is atomic within
  the same PostgreSQL connection pool
- The challenge's `ProjectionDaemon` interface (`run_forever`, `_process_batch`)
  maps directly to a background asyncio loop

**Daemon lifecycle**:
```python
daemon_task = asyncio.create_task(daemon.run_forever(poll_interval_ms=100))
# Started on MCP server startup; cancelled on shutdown
```

**Fault tolerance design**:
1. Each projection handler is called inside `try/except Exception`
2. On handler failure: increment retry counter for that event+projection pair
3. After 3 failures: log error with event_id + projection_name, record in a
   `projection_errors` table (see data-model.md), skip event for this projection
4. Daemon continues to next event — it NEVER raises to the outer loop
5. `projection_errors` table enables post-mortem and manual replay

**Checkpoint update**: Updated after each successful batch (not per-event)
to reduce write amplification. Batch size configurable (default 500 events).

---

## R-004 — UpcasterRegistry Pattern

**Decision**: Decorator-based registry with automatic chain traversal on event load.

**Rationale**:
The challenge provides the exact `UpcasterRegistry` class signature with
`@registry.register(event_type, from_version)` decorator pattern. This maps
directly to a dict-based registry keyed on `(event_type, version)` tuples.

**Key implementation notes**:
1. The registry MUST be a singleton (module-level instance) to ensure all
   upcasters registered with `@registry.register(...)` in `upcasters.py` are
   available when `EventStore.load_stream()` calls `registry.upcast(event)`
2. `StoredEvent.with_payload(new_payload, version=v+1)` must return a new
   `StoredEvent` instance (immutable pattern) — never mutate the original
3. `load_stream()` MUST call `registry.upcast(event)` for every loaded event
   before returning it to callers — the upcasting must be transparent
4. The raw DB payload MUST be read but never written by the upcast path —
   immutability test verifies this

**CreditAnalysisCompleted v1→v2 inference strategy**:
- `model_version`: infer from `recorded_at` timestamp using a known deployment
  timeline lookup table (e.g., events before 2026-01-01 → `"legacy-pre-2026"`)
- `confidence_score`: `None` — genuinely unknown; fabrication would create false
  precision in regulatory audit records, making the null-vs-fabrication choice
  a compliance decision, not a convenience decision
- `regulatory_basis`: infer from the regulation_set_version active at `recorded_at`

**DecisionGenerated v1→v2 inference strategy**:
- `model_versions{}`: reconstruct by loading each session's `AgentContextLoaded`
  event from the `AgentSession` stream; this requires a store lookup inside the
  upcaster — document the O(n) performance implication in DESIGN.md where
  n = number of contributing agent sessions (typically 3–4, bounded)

---

## R-005 — MCP Python SDK: FastMCP vs Low-Level

**Decision**: FastMCP (decorator-based API from the `mcp` Python SDK).

**Rationale**:
- FastMCP's `@mcp.tool()` and `@mcp.resource()` decorators map directly to the
  challenge's "8 tools + 6 resources" structure
- FastMCP handles protocol-level serialisation; implementation focuses on
  domain logic
- Pydantic models integrate natively for tool input validation (matching FR-009's
  "Schema validation via Pydantic" requirement for `submit_application`)
- Error responses: FastMCP supports raising exceptions that serialise to structured
  MCP error responses — use custom exception types (`OptimisticConcurrencyError`,
  `DomainError`, `PreconditionFailed`) that serialise to typed error objects

**Tool error contract**:
All tool errors MUST return a structured JSON object:
```json
{
  "error_type": "OptimisticConcurrencyError",
  "message": "Stream version mismatch",
  "stream_id": "loan-app-123",
  "expected_version": 3,
  "actual_version": 5,
  "suggested_action": "reload_stream_and_retry"
}
```
This is not just a string — it is a typed object the consuming LLM can reason about.

---

## R-006 — Cryptographic Hash Chain (SHA-256)

**Decision**: `hashlib.sha256` (stdlib) with canonical JSON serialisation for
deterministic hashing.

**Rationale**:
- No external crypto library needed; stdlib `hashlib` is sufficient for SHA-256
- Canonical serialisation: `json.dumps(payload, sort_keys=True, ensure_ascii=False)`
  produces deterministic output across Python versions
- Chain construction: `new_hash = sha256(previous_hash.encode() + payload_hash)`
  where each event's contribution is `sha256(json.dumps(event.payload, sort_keys=True))`

**Hash chain integrity algorithm**:
```
previous_hash = "0" * 64  (genesis hash for first check)
for event in events_since_last_check:
    event_hash = sha256(canonical_json(event.payload))
    chain_hash = sha256(previous_hash + event_hash)
    previous_hash = chain_hash
new_hash = chain_hash
```

**Tamper detection**: If `sha256(previous_hash + recalculated_event_hashes) !=
stored_integrity_hash`, then `tamper_detected = True`, `chain_valid = False`.

---

## R-007 — Enterprise Stack Translation

*This section fulfils the user requirement for an "Enterprise Stack Translation"
section mapping the PostgreSQL implementation to Marten/Wolverine (.NET) and
EventStoreDB. This content appears in expanded form in DESIGN.md Section 5.*

### PostgreSQL Implementation → EventStoreDB 24.x

| This Implementation | EventStoreDB Equivalent | What EventStoreDB Gives You for Free |
|---------------------|------------------------|--------------------------------------|
| `events` table with `stream_id + stream_position` | Native stream storage; each stream is a first-class object | No schema management; built-in stream compaction and truncation |
| `event_streams.current_version` + `SELECT FOR UPDATE` | `expectedRevision` parameter on `AppendToStreamAsync` | Native OCC at the protocol level; no application-layer lock needed |
| `load_stream(stream_id)` via SQL `WHERE stream_id = $1` | `ReadStreamAsync(streamName, StreamPosition.Start)` | Optimised stream reads; forward and backward cursor support |
| `load_all(from_global_position)` async generator | `$all` stream subscription with `FromAll(Position.Start)` | Built-in global ordering; persistent subscriptions survive process restart |
| `ProjectionDaemon` polling `global_position` | EventStoreDB persistent subscriptions with consumer group checkpointing | Distributed checkpoint management; competing consumers across nodes |
| `projection_checkpoints` table | Persistent subscription checkpoint stored in EventStoreDB itself | No external checkpoint storage; auto-resume on reconnect |
| `outbox` table + manual publisher | EventStoreDB's built-in persistent subscriptions act as the outbox | No separate outbox needed; at-least-once delivery is native |
| `archive_stream` setting `archived_at` | Stream soft-deletion / truncation via `$tb` (truncate before) metadata | Native stream lifecycle management |

**What EventStoreDB gives you that this implementation works harder to achieve**:
1. **Persistent subscriptions**: EventStoreDB's competing consumer groups handle
   multi-node projection distribution natively; this implementation's
   `ProjectionDaemon` requires a coordination primitive (e.g., Redis SETNX or
   PostgreSQL advisory lock) to achieve the same on multiple nodes.
2. **Built-in $all stream**: EventStoreDB's global stream is a native construct;
   this implementation's `global_position BIGINT GENERATED ALWAYS AS IDENTITY`
   achieves the same ordering but requires careful handling of identity gaps under
   transaction rollbacks.
3. **Native gRPC streaming**: EventStoreDB streams events to subscribers via gRPC
   without polling; this implementation polls at 100 ms intervals, adding up to
   100 ms latency to the projection pipeline.

### PostgreSQL Implementation → Marten 7.x + Wolverine

| This Implementation | Marten/Wolverine Equivalent | Notes |
|---------------------|-----------------------------|-------|
| `EventStore.append()` with OCC | `IDocumentSession.Events.Append(streamKey, expectedVersion, events)` followed by `SaveChangesAsync()` | Marten handles the `mt_events` table schema; same OCC semantics |
| `LoanApplicationAggregate.load()` + `_apply()` | `IQuerySession.Events.AggregateStreamAsync<LoanApplication>(streamId)` | Marten calls the `Apply(event)` methods via reflection; same pattern |
| `UpcasterRegistry` + `@registry.register` | `IEventTransformation` implementations registered in `StoreOptions` | Marten's upcasting uses the same `IEventTransformation` → `from_version → to_version` chain |
| `ProjectionDaemon.run_forever()` | Marten's Async Daemon (`IDocumentStore.BuildProjectionDaemon()`) | Marten's daemon supports distributed execution across multiple nodes via leader election |
| `ApplicationSummary` projection | `IProjection` or `MultiStreamProjection<ApplicationSummary>` | Marten handles SQL upserts; developer writes only `Apply(event)` methods |
| `ComplianceAuditView.get_compliance_at(timestamp)` | `IQuerySession.Events.AggregateStreamAsync<ComplianceRecord>(id, timestamp: asOf)` | Marten supports native temporal queries via `asOf` parameter |
| `generate_regulatory_package()` | Custom — Marten has no native regulatory package concept | Marten provides the event query primitives; package assembly is application code in both cases |
| `run_integrity_check()` + SHA-256 chain | Custom — no native hash chain in Marten | Marten provides no cryptographic audit chain; this is a domain feature in both stacks |

**Advising a .NET client**:
When a client already has a Marten/Wolverine stack, the business logic (aggregates,
business rules, upcasters) ports directly: the `_apply` pattern becomes `Apply`
methods, the `UpcasterRegistry` becomes `IEventTransformation` registrations, and
command handlers become Wolverine message handlers. The event schema (stream IDs,
payload shapes, version numbers) is identical. The migration effort is infrastructure
rewiring, not domain rewrite.

**Advising a client considering EventStoreDB**:
EventStoreDB eliminates the need for: (a) the `event_streams` table, (b) the
`global_position` identity column, (c) the `ProjectionDaemon` polling loop, and
(d) the `outbox` table. The trade-off is operational complexity of a dedicated
event store service vs. the "Postgres is everywhere" deployment simplicity.
For teams already running PostgreSQL, the custom implementation in this challenge
is the right first deployment; EventStoreDB becomes the migration target when
throughput exceeds ~10,000 events/second sustained.

---

## R-008 — Snapshot Strategy for ComplianceAuditView Temporal Queries

**Decision**: Event-count trigger — snapshot taken every 100 compliance events
per application; stored in `compliance_snapshots` table.

**Implementation**:
```sql
CREATE TABLE compliance_snapshots (
  application_id    TEXT NOT NULL,
  snapshot_position BIGINT NOT NULL,   -- global_position of last event included
  snapshot_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  state_json        JSONB NOT NULL,
  PRIMARY KEY (application_id, snapshot_position)
);
CREATE INDEX idx_compliance_snapshots_app ON compliance_snapshots
  (application_id, snapshot_position DESC);
```

**Temporal query algorithm** (`get_compliance_at(application_id, timestamp)`):
1. Find nearest preceding snapshot: `SELECT * FROM compliance_snapshots WHERE
   application_id = $1 AND snapshot_at <= $2 ORDER BY snapshot_at DESC LIMIT 1`
2. If no snapshot: replay from position 0
3. Load compliance events from `snapshot_position` to events where
   `recorded_at <= $2`
4. Apply events to snapshot state; return result

**Snapshot invalidation**: Snapshots are never invalidated — they represent
historical truth. On `rebuild_from_scratch()`, the `compliance_snapshots` table
is also truncated and rebuilt. Snapshots for an application are append-only;
they are never updated or deleted outside of a full rebuild.

**Why 100 events**: The average Apex application has ~15–20 compliance events
(checks × rules). 100 events ≈ 5–7 applications' worth of compliance data
in the stream, making snapshots rare enough to not cause storage pressure
while keeping `get_compliance_at` replay cost bounded.

---

## R-009 — Gas Town Agent Memory Reconstruction

**Decision**: Token-budget-aware summarisation with verbatim preservation of
the last 3 events and any PENDING/ERROR state events.

**Reconstruction algorithm**:
1. Load all events from `AgentSession` stream for `agent_id + session_id`
2. Walk events in order; separate into: "old context" (all except last 3 +
   PENDING/ERROR) vs "preserve verbatim" (last 3 + PENDING/ERROR)
3. For "old context" events: generate one prose sentence per event type
   (e.g., "Agent loaded context from stream position 0 at 14:32 UTC")
4. Count tokens in verbatim section; subtract from `token_budget`
5. Fill remaining budget with prose summary of old context (truncate oldest first)
6. If last event has no corresponding completion event: set
   `session_health_status = "NEEDS_RECONCILIATION"`; include explicit
   "INCOMPLETE ACTION" marker at the end of context_text

**NEEDS_RECONCILIATION detection**: An event is "partial" if it belongs to a
class requiring a paired completion (e.g., `CreditAnalysisRequested` without
a subsequent `CreditAnalysisCompleted` in the same session stream for the same
`application_id`).

---

## R-010 — DOMAIN_NOTES.md Pre-Implementation Content Outline

*The following question answers are research-level findings that the
`DOMAIN_NOTES.md` document must develop in full before any implementation
begins (Principle VI, Principle VIII Phase Gate 0).*

**Q1 — EDA vs ES distinction**:
A LangChain callback trace is EDA — events are fired for observability and can
be dropped; they are not the source of truth. Redesigning with The Ledger:
replace callbacks with `EventStore.append()` calls; the agent's context is
reconstructed from the stream on restart; the trace IS the state.

**Q2 — Rejected aggregate boundary**:
Alternative considered: merging `ComplianceRecord` into `LoanApplication`.
Rejected because: a compliance check and a loan state transition can occur
concurrently (two compliance agents checking different rules in parallel).
Merged aggregate would require a single lock on the loan stream for all compliance
work, serialising what should be parallel writes. Separate aggregate allows
N compliance agents to write to `compliance-{id}` while the loan state machine
progresses independently.

**Q3 — Concurrency trace**:
Both agents acquire: `SELECT current_version FROM event_streams WHERE stream_id =
'loan-app-123' FOR UPDATE`. One wins the row lock. It reads version=3, checks
expected_version=3 ✓, inserts event at stream_position=4, updates
current_version=4, commits. The second agent then acquires the lock, reads
version=4, checks expected_version=3 ✗, rolls back, raises
`OptimisticConcurrencyError(expected=3, actual=4)`.

**Q4 — Projection lag consequence**:
The system serves the stale read from the `ApplicationSummary` projection. The
UI receives the old credit limit value. Mitigation: the `ledger://ledger/health`
response includes current projection lag; the UI can display a staleness indicator
("Data as of N ms ago") when lag exceeds a configurable threshold (e.g., 1,000 ms).
The UI should never block on projection consistency — it accepts eventual
consistency and communicates it.

**Q5 — CreditAnalysisCompleted v1→v2 upcaster**:
See R-004 above. Inference: `model_version` from timestamp lookup; `confidence_score`
= null (regulatory compliance reason documented); `regulatory_basis` from
regulation version active at `recorded_at`.

**Q6 — Marten Async Daemon parallel**:
Coordination primitive: PostgreSQL advisory lock (`pg_try_advisory_lock(hashtext('projection-daemon-leader'))`)
for leader election across multiple nodes. The failure mode it guards against:
split-brain projection processing where two daemon nodes process the same
global_position range and apply duplicate updates to projection tables. Only
the lock holder processes events; others stand by and poll the lock periodically.
