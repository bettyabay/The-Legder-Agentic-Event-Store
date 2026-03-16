# DOMAIN_NOTES.md — The Ledger: Domain Reasoning & FDE Analysis

**Apex Financial Services — Agentic Event Store & Enterprise Audit Infrastructure**
Version: 1.0 | Date: 2026-03-16

---

## 1. EDA vs. Event Sourcing Distinction

**Question:** A component uses callbacks (like LangChain traces) to capture event-like
data. Is this Event-Driven Architecture (EDA) or Event Sourcing (ES)? If redesigned
using The Ledger, what exactly would change?

**Answer:**

LangChain trace callbacks are **EDA**, not ES. The distinction is fundamental:

| Dimension | EDA (LangChain traces) | Event Sourcing (The Ledger) |
|-----------|----------------------|-----------------------------|
| Storage | Logs to tracing backend; may be dropped | Append-only PostgreSQL table; never dropped |
| Source of truth | The in-memory model state IS the truth; traces annotate it | The event stream IS the truth; state is derived by replay |
| Reproducibility | Cannot reconstruct exact state at T-1 | Full state reconstruction at any timestamp via `load_stream()` |
| Mutability | Log entries can be rotated, truncated | `UNIQUE(stream_id, stream_position)` prevents modification |
| Concurrency | No OCC; callbacks fire and forget | OCC via `expected_version`; conflicting writes rejected |

**What would change in a redesign:**
1. Every LangChain callback (`on_llm_start`, `on_chain_end`, etc.) would emit a
   `BaseEvent` subclass appended to an `AgentSession` stream via `EventStore.append()`.
2. The agent's "current context" would be reconstructed by replaying its stream
   (the Gas Town pattern) rather than maintained in process memory.
3. The callback handler would pass `expected_version=agent.version` to detect
   concurrent updates (rare but possible in parallel LangGraph execution).
4. **What you gain:** temporal queries, cross-run analysis, crash recovery,
   compliance audit trail — all for free from the existing infrastructure.

---

## 2. Aggregate Boundary Consideration

**The boundary I considered and rejected:**

**Rejected: Merging ComplianceRecord into LoanApplication**

Initial reasoning: compliance is logically "part of" the loan lifecycle. A single
stream simplifies the `assert_compliance_clearance()` check — the aggregate already
has all compliance data without a cross-stream reference.

**Why rejected — the coupling problem it would create:**

Under the rejected model:
```
loan-{id} stream receives:
  - ApplicationSubmitted (1 write)
  - CreditAnalysisRequested (1 write)
  - CreditAnalysisCompleted (1 write, from credit agent)
  - FraudScreeningCompleted (1 write, from fraud agent)
  - ComplianceRulePassed × N (N writes, from compliance agent)
  - ComplianceRuleFailed × M (M writes, from compliance agent)
  - DecisionGenerated (1 write, from orchestrator)
```

With 10 compliance rules per application and 4 concurrent agents writing to the
same `loan-{id}` stream, the OCC error rate rises from ~2% to ~35%+. Each error
requires full aggregate reload (all events since position 0). At 1,000 apps/hour
this generates ~35,000 wasted reloads/hour — a scaling cliff.

**Chosen boundary prevents:** compliance writes competing with credit/fraud writes
on the same stream. The LoanApplication aggregate holds only
`compliance_clearance_issued: bool` — a single fact that arrives once, atomically,
via `ComplianceClearanceIssued`.

---

## 3. Concurrency in Practice — The Double-Decision Trace

**Scenario:** Two AI agents simultaneously process application `app-ABC` and both
call `append_events` with `expected_version=3`.

**Exact sequence:**

1. Agent A reads `event_streams` WHERE `stream_id='loan-app-ABC'` → `current_version=3`
2. Agent B reads `event_streams` WHERE `stream_id='loan-app-ABC'` → `current_version=3`
3. Agent A executes:
   ```sql
   BEGIN;
   SELECT current_version FROM event_streams WHERE stream_id='loan-app-ABC' FOR UPDATE;
   -- Returns 3; matches expected_version=3; proceeds
   INSERT INTO events (stream_id, stream_position=4, ...) VALUES (...);
   UPDATE event_streams SET current_version=4 WHERE stream_id='loan-app-ABC';
   COMMIT;
   ```
4. Agent B executes:
   ```sql
   BEGIN;
   SELECT current_version FROM event_streams WHERE stream_id='loan-app-ABC' FOR UPDATE;
   -- Returns 4 (Agent A has committed); does NOT match expected_version=3
   ROLLBACK;
   ```
   → EventStore raises `OptimisticConcurrencyError(expected=3, actual=4)`

5. **Agent B receives** `OptimisticConcurrencyError` with:
   - `suggested_action: "reload_stream_and_retry"`
   - `expected_version: 3`
   - `actual_version: 4`

6. **Agent B must:** reload the stream (`load_stream("loan-app-ABC")`), observe
   Agent A's decision, determine whether its own analysis is still relevant
   (business logic), and either retry with `expected_version=4` or abort.

**Total events in stream:** 4 (not 5) — the constraint is preserved.

---

## 4. Projection Lag and Its Consequences

**Scenario:** ApplicationSummary projection lags 200ms. A loan officer queries
"available credit limit" immediately after an agent commits a disbursement event.
They see the old limit.

**What the system does:**

The `ApplicationSummary` projection is eventually consistent with a 200ms typical lag.
The loan officer's query hits the projection table (`SELECT * FROM application_summary`),
which may not yet reflect the disbursement event.

**Production handling:**

1. The `ledger://applications/{id}` resource description documents its eventual consistency
   explicitly: "Returns current state from ApplicationSummary projection (max lag 500ms)."
2. For operations where stale data is dangerous (e.g., approving a second disbursement),
   the command handler — not the query — is the source of truth. The command handler
   reconstructs aggregate state from the event stream (`load_stream()`), not the projection.
   A second disbursement command that conflicts is caught by OCC or state machine guards
   regardless of what the projection shows.
3. For the loan officer UI specifically: the response includes `last_event_at` timestamp.
   The UI can display "Last updated: 200ms ago" and add a "Refresh" affordance.

**How we communicate this to the UI:**
The `ledger://applications/{id}` resource response includes:
```json
{"last_event_at": "2026-03-16T10:34:55Z", "projection_lag_ms": 180}
```
The UI uses `projection_lag_ms` to show a freshness indicator.

---

## 5. The Upcasting Scenario

**Original v1 schema (2024):**
```json
{"application_id": "...", "decision": "APPROVE", "reason": "..."}
```

**Required v2 schema (2026):**
```json
{"application_id": "...", "decision": "APPROVE", "reason": "...",
 "model_version": "...", "confidence_score": 0.85, "regulatory_basis": "..."}
```

**Upcaster implementation:**
```python
@registry.register("CreditDecisionMade", from_version=1)
def upcast_v1_to_v2(payload: dict) -> dict:
    return {
        **payload,
        "model_version": "legacy-pre-2026",   # temporal inference
        "confidence_score": None,              # genuinely unknown
        "regulatory_basis": None,              # requires store lookup; deferred
    }
```

**Inference strategy for `model_version` on historical events:**

Historical events recorded before 2026-01-01 definitionally predate the model versioning
schema. `"legacy-pre-2026"` is the accurate label, not a fabrication. Error rate: 0%.

**Why `confidence_score` is `null`, not fabricated:**
The confidence floor business rule (`confidence_score < 0.6 → REFER`) would fire
incorrectly on fabricated values. If a historical event is fabricated with `0.75`,
it appears to be a valid high-confidence decision. If the true historical confidence
was 0.45, the REFER recommendation was warranted but is now invisible. `null` is the
honest answer; it tells downstream systems "this fact is unknown for this event."

---

## 6. Marten Async Daemon Parallel

**Question:** How would you achieve distributed projection execution across multiple
nodes in Python?

**Pattern:** Distributed lock with PostgreSQL advisory locks.

```python
# Each daemon node attempts to acquire an advisory lock on the projection name
async with pool.acquire() as conn:
    acquired = await conn.fetchval(
        "SELECT pg_try_advisory_lock($1)",
        hash("projection_name") % (2**31),
    )
    if not acquired:
        # Another node owns this projection; skip
        return
    await self._process_batch()
    # Lock released when connection returns to pool
```

**Coordination primitive:** PostgreSQL advisory locks (session-level).
- Only one node processes a given projection at a time.
- If the owning node crashes, the lock is released automatically when its connection closes.
- Recovery: the next polling cycle on any surviving node acquires the lock.

**Failure mode guarded against:** Split-brain projection state — two nodes processing
the same events in parallel would create duplicate or out-of-order projection updates.
The advisory lock ensures serialised processing with automatic crash recovery.

**Limitation vs. Marten:** Marten 7.0 uses a distributed coordination protocol with
explicit shard assignment, allowing multiple projections to be processed in parallel
across shards. Our implementation is single-node-per-projection (not per-shard).
For full horizontal scale, we would need a shard assignment table and lock-per-shard.

---

## 7. Enterprise Stack Translation

### The Ledger → Marten 7.x + Wolverine (.NET)

| The Ledger (Python/PostgreSQL) | Marten / Wolverine (.NET) | Notes |
|-------------------------------|--------------------------|-------|
| `EventStore.append()` | `IDocumentSession.Events.Append()` | Marten wraps asyncpg-style OCC internally |
| `expected_version` parameter | `expectedVersion` in `AppendStream()` | Identical concept |
| `ProjectionDaemon` | Marten Async Daemon (`IHostedService`) | Marten provides this as a production-grade hosted service |
| `Projection.handle()` per event | `IProjection.Apply()` per event | Direct equivalent |
| `projection_checkpoints` table | Marten daemon checkpoint store | Marten manages this automatically |
| `UpcasterRegistry` | Marten `IEventSourcingOptions.Upcast()` | Marten has first-class upcasting support |
| `compliance_snapshots` | Marten snapshot store (`SnapshotOnEvery<T>`) | Marten handles this automatically |
| `EventStore.load_all()` | `IQuerySession.Events.QueryAllRawEvents()` | Wolverine connects this to message bus |
| `outbox` table | Wolverine Outbox (built-in) | Wolverine integrates outbox at framework level |
| MCP tools (command side) | Wolverine `IMessageBus.PublishAsync()` | Commands published to Wolverine's message bus |
| MCP resources (query side) | Marten `IQuerySession.LoadAsync<T>()` | Reads from projected views |

### The Ledger → EventStoreDB 24.x

| The Ledger (Python/PostgreSQL) | EventStoreDB | Notes |
|-------------------------------|--------------|-------|
| `loan-{id}` stream | `loan-{id}` stream name | Identical string format |
| `EventStore.append()` with OCC | `AppendToStreamAsync()` with `expectedRevision` | API-level equivalent |
| `events.global_position` | `$all` stream position | EventStoreDB `$all` is the global log |
| `EventStore.load_all()` | Subscribe to `$all` persistent subscription | Server-side, push-based |
| `ProjectionDaemon` | Persistent subscriptions + projections server | Server-side execution |
| `event_streams` table | Stream metadata API | EventStoreDB tracks stream metadata natively |
| `projection_checkpoints` | Checkpoint stored in persistent subscription | Server-managed |
| `outbox` table | Not needed — persistent subscriptions are durable | EventStoreDB eliminates the outbox requirement |
| `archive_stream()` | EventStoreDB soft-delete / tombstone | `DeleteStreamAsync()` |
| Temporal queries via snapshots | EventStoreDB projections to separate streams | EventStoreDB supports projections to derived streams |

**FDE Advisory Summary:**

When a client asks "should we use Marten or EventStoreDB?":
- **Marten**: Choose if the client is on .NET, already has PostgreSQL, and wants minimal
  new infrastructure. Marten is the lowest-friction path for a .NET enterprise team.
- **EventStoreDB**: Choose if the client needs >100K events/second, wants server-side
  projections without a custom daemon, or is building a purpose-built event sourcing
  architecture where an extra infrastructure process is acceptable.
- **PostgreSQL + asyncpg (this implementation)**: Choose for Python teams, rapid
  FDE deployment, or when adding an event sourcing layer to an existing PostgreSQL
  application without new infrastructure.
