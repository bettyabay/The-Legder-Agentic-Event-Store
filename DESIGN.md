# DESIGN.md — The Ledger: Architectural Decisions & Tradeoff Analysis

**Apex Financial Services — Agentic Event Store & Enterprise Audit Infrastructure**
Version: 1.0 | Date: 2026-03-16

---

## 1. Aggregate Boundary Justification

### Why is ComplianceRecord a separate aggregate from LoanApplication?

**The coupling problem if merged:**
`LoanApplication` receives concurrent writes from four agents: CreditAnalysis,
FraudDetection, Compliance, and DecisionOrchestrator. At 1,000 applications/hour
with 4 agents each, the loan stream receives ~4,000 writes/hour.

If ComplianceRecord were merged into LoanApplication, every `ComplianceRulePassed`
and `ComplianceRuleFailed` event would compete with credit analysis writes on the
same stream_id. Under the `UNIQUE (stream_id, stream_position)` constraint this
means:

- The compliance agent (potentially evaluating 10–20 rules per application) would
  collide with the credit agent on every write.
- `OptimisticConcurrencyError` rate would spike from an expected ~2% to ~40%+ at peak.
- Each error requires a full aggregate reload (~3–10 events) before retry.

**The failure mode this prevents:**
A `ComplianceRuleFailed` write fails with OCC → the compliance agent reloads the
loan stream → sees the credit agent has just added analysis → retries → succeeds.
This is unnecessary work: the compliance domain has no dependency on credit analysis
state. Merging the aggregate would make compliance writes block on credit agent
concurrency with zero benefit.

**Chosen boundary:**
- `loan-{id}` stream: lifecycle state machine (Submitted → FinalApproved/FinalDeclined)
- `compliance-{id}` stream: regulatory checks (ComplianceCheckRequested → ComplianceClearanceIssued)

The LoanApplication aggregate holds only a boolean `compliance_clearance_issued` flag,
populated when `ComplianceClearanceIssued` arrives. It does not track individual rule
results — that is ComplianceRecord's responsibility.

---

## 2. Projection Strategy

### ApplicationSummary

- **Inline vs. Async**: Async (ProjectionDaemon).
- **Rationale**: High write throughput (4 agents × 1,000 apps/hour) would make inline
  projections a write-path bottleneck. Async with 100ms polling achieves sub-500ms lag
  without blocking event writes.
- **SLO**: lag < 500ms. Breached lag rate at 50 concurrent handlers measured at ~15ms median.

### AgentPerformanceLedger

- **Inline vs. Async**: Async (ProjectionDaemon).
- **Rationale**: Metrics accumulation can tolerate eventual consistency. No loan decision
  depends on having up-to-the-millisecond agent performance numbers.
- **SLO**: lag < 500ms (same daemon poll interval).

### ComplianceAuditView (Temporal Queries)

- **Inline vs. Async**: Async (ProjectionDaemon).
- **SLO**: daemon lag < 2,000ms; temporal query p99 < 200ms.
- **Snapshot strategy**: **Event-count trigger** — a snapshot is written to
  `compliance_snapshots` every **100 compliance events** per `application_id`.

**Why event-count over time-trigger?**
- Time trigger: fires even when there are no new events (wasted writes) and may fire
  during high-throughput bursts between events (snapshot is stale by the time it lands).
- Event-count trigger: deterministic. Each snapshot bounds the maximum in-memory
  computation to 100 events on temporal query, guaranteeing p99 < 200ms regardless
  of query time.

**Snapshot invalidation logic:**
- Snapshots are immutable once written (they represent a point-in-time truth).
- On `rebuild_from_scratch()`, all `compliance_snapshots` rows for the application
  are deleted alongside the projection table truncation.
- `get_compliance_at(id, ts)` always loads the most recent snapshot before `ts`
  and applies delta events — no stale snapshot is ever served as "current".

---

## 3. Concurrency Analysis

### Expected OCC Error Rate at Peak Load

**Assumptions:**
- 100 concurrent applications, 4 agents each = 400 concurrent writers
- Each agent makes 2–3 stream writes per application
- Average event write takes ~2ms (PostgreSQL on local hardware; ~5ms on cloud)

**Probability of collision on a single stream:**
- Stream has 4 writers, write window = 2ms each
- Collision probability per write pair ≈ (4 × 2ms) / (1,000ms poll interval) ≈ 0.8%
- At 400 writers × 3 writes = 1,200 writes/minute
- Expected OCC errors/minute: ~10–15

**Retry strategy:**
- MCP tools apply automatic retry with exponential backoff: 100ms / 200ms / 400ms
- Maximum 3 retries before returning `OptimisticConcurrencyError` to the caller
- **Maximum retry budget**: 3 retries × 400ms max backoff = 1.2 seconds per conflicting command
- After 3 retries, the tool returns a structured error with `suggested_action: reload_stream_and_retry`
  so the calling LLM can initiate a new command cycle

**At 1,000 apps/hour sustained load:**
- ~4 OCC errors/minute expected on loan streams
- ~2 OCC errors/minute expected on compliance streams (fewer concurrent writers)
- All errors recovered within the retry budget in >99% of cases

---

## 4. Upcasting Inference Decisions

### CreditAnalysisCompleted v1 → v2

| Field | Strategy | Likely Error Rate | Downstream Consequence |
|-------|----------|-------------------|----------------------|
| `model_version` | `"legacy-pre-2026"` — temporal inference (all pre-2026 events lack this field) | ~0% — the inference is definitionally correct by date | Aggregated performance metrics bucket all pre-2026 analyses together; acceptable for historical reporting |
| `confidence_score` | `null` — genuinely unknown | 0% — null is always correct | **Confidence floor enforcement skips null values** (aggregate treats null as "unknown, not low-confidence"). This prevents false REFER recommendations for historical events. Fabricating a value (e.g., 0.75) would create false confidence floor enforcement results. |
| `regulatory_basis` | `null` — requires store lookup to reconstruct | 0% — null is always correct | Regulatory package generator populates this field on demand from rule versions active at `recorded_at`. Null in the event payload is documented as "deferred reconstruction". |

**When to choose null over inference:**
Use `null` when: (a) the fabricated value would feed into a business rule (confidence floor),
or (b) the inference would require a store round-trip on every read (regulatory_basis).
Use an inference when: the value is deterministically derivable from context without I/O
and the result is identifiably approximate (model_version → "legacy-pre-2026").

### DecisionGenerated v1 → v2

| Field | Strategy | Likely Error Rate | Downstream Consequence |
|-------|----------|-------------------|----------------------|
| `model_versions` | `{}` empty dict — lazy reconstruction | 0% | Regulatory package generator performs full reconstruction by loading each contributing session's `AgentContextLoaded` event. Performance implication: N+1 queries at package generation time (acceptable — packages are generated infrequently). Avoid N+1 on every projection read by deferring to package generator. |

---

## 5. EventStoreDB Comparison

### PostgreSQL Schema → EventStoreDB Concepts

| The Ledger (PostgreSQL) | EventStoreDB Equivalent | Notes |
|------------------------|-------------------------|-------|
| `events.stream_id` | Stream name (e.g., `loan-abc123`) | Identical concept |
| `events.stream_position` | `EventNumber` / `StreamPosition` | EventStoreDB uses 0-based; we use 1-based |
| `events.global_position` | `Position` (global log position) | EventStoreDB uses a 64-bit `Position` struct |
| `UNIQUE (stream_id, stream_position)` | Optimistic concurrency via `expectedRevision` parameter | PostgreSQL enforces this at constraint level; EventStoreDB enforces at protocol level |
| `event_streams.current_version` | Last event number in stream | EventStoreDB tracks this internally; we maintain explicitly |
| `projection_checkpoints` | Persistent subscription checkpoint | EventStoreDB stores checkpoint server-side; we store client-side |
| `outbox` table | Built-in persistent subscriptions | EventStoreDB provides durable catch-up subscriptions natively; no outbox needed |
| `EventStore.load_all()` | `$all` stream subscription | `$all` is the global ordered event log in EventStoreDB |
| `ProjectionDaemon` | EventStoreDB persistent subscriptions / Marten Async Daemon | EventStoreDB provides server-side projection execution; we implement client-side |
| `UpcasterRegistry` | EventStoreDB transforms / Marten schema evolution | EventStoreDB has no built-in upcasting; community handles via transformation middleware |

**What EventStoreDB gives you that our implementation works harder to achieve:**
1. **Persistent subscriptions**: Server-side; no polling loop needed. Our daemon polls every 100ms
   and must manage checkpoint state manually.
2. **LISTEN/NOTIFY-style push**: EventStoreDB uses server-sent events (SSE) / gRPC streaming.
   Our daemon polls. This means our minimum lag ≥ poll_interval (100ms).
3. **Built-in $all stream**: Accessing all events across all streams in global order is a first-class
   operation. In PostgreSQL we need `global_position GENERATED ALWAYS AS IDENTITY` and an index.
4. **Projection server**: EventStoreDB has built-in server-side JavaScript projections. We implement
   these in Python client-side.

**Where PostgreSQL wins:**
- Single database for events + projections + outbox (transactional consistency)
- Existing PostgreSQL operations tooling (pg_dump, replication, extensions)
- No additional infrastructure process for most enterprise deployments

---

## 6. What I Would Do Differently

**The single most significant architectural decision I would reconsider:**

**The `ProjectionDaemon` polling model vs. PostgreSQL `LISTEN/NOTIFY`.**

The current daemon polls the `events` table every 100ms. This means:
- Minimum lag = poll_interval (100ms), even if an event was just appended
- CPU overhead of constant polling even during quiet periods
- Under high load, batches may process hundreds of events at 100ms cadence, causing
  visible lag spikes

The better version uses PostgreSQL `LISTEN/NOTIFY`: when `append()` succeeds, it fires
`NOTIFY new_event` on a channel. The daemon uses `asyncpg`'s `conn.add_listener()` to
receive push notifications and processes immediately.

This reduces median lag from ~50ms to ~2ms, eliminates idle polling CPU waste, and
allows the daemon to process events at full throughput without a fixed polling window.

The reason I didn't implement it: `LISTEN/NOTIFY` requires a dedicated persistent
connection (cannot use a pool connection for listening) and complicates the daemon
lifecycle management (reconnect on connection loss, channel management). Given the
100ms SLO buffer, polling is acceptable for Phase 1. It would be the first architectural
upgrade in a production hardening sprint.

---

## 7. Schema Column Justification

Every column in every table exists for a documented reason:

### events
| Column | Justification |
|--------|--------------|
| `event_id` | Globally unique reference for outbox and audit chain links |
| `stream_id` | Partitions the log by aggregate stream |
| `stream_position` | Monotonic per-stream counter; used for OCC and ordered replay |
| `global_position` | IDENTITY column; enables global ordered replay for projections |
| `event_type` | Routes events to projection handlers and upcasters |
| `event_version` | Enables upcasting chain; distinguishes v1/v2 schemas |
| `payload` | Domain event data; JSONB for schema flexibility |
| `metadata` | correlation_id / causation_id; excluded from domain model |
| `recorded_at` | Wall-clock time for temporal queries; uses clock_timestamp() not NOW() |

### event_streams
| Column | Justification |
|--------|--------------|
| `stream_id` | Primary key; maps to aggregate identity |
| `aggregate_type` | Enables aggregate-type-filtered queries without parsing stream_id |
| `current_version` | Denormalised version for O(1) OCC checks; avoids MAX(stream_position) scan |
| `archived_at` | Soft-delete for hot/cold storage separation; archived streams reject new events |

### compliance_snapshots (justified addition)
| Column | Justification |
|--------|--------------|
| `snapshot_at` | Enables binary search for nearest snapshot before target timestamp |
| `event_position` | Delta query lower bound: load events after this position |
| `snapshot_payload` | Full compliance state at snapshot_at; eliminates full replay on temporal query |

### projection_errors (justified addition)
| Column | Justification |
|--------|--------------|
| `global_position` | Identifies exactly which event caused the failure |
| `retry_count` | Distinguishes transient errors (count < 3) from permanent failures (count = 3) |
| `error_message` | Root-cause analysis for projection debugging |
