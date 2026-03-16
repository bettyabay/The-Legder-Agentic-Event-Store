# Tasks: The Ledger — Agentic Event Store & Enterprise Audit Infrastructure

**Input**: Design documents from `specs/001-ledger-event-store/`
**Branch**: `001-ledger-event-store` | **Date**: 2026-03-16

**Available docs**: plan.md ✅ · spec.md ✅ · data-model.md ✅ · research.md ✅ · quickstart.md ✅ · contracts/ ✅

**Tests**: Mandatory — 5 test files required by spec (FR-003, FR-004, SC-001–SC-009) and
Constitution Principle IV. Tests are embedded in their respective challenge phases.

**Organization**: Tasks are grouped by the 6 challenge phases (mandated by Constitution
Principle VIII — phase gates). Each task is tagged [USN] to show which user story it
delivers. Multiple story tags on one task indicate shared infrastructure.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: User story this task contributes to (US1–US6, from spec.md priorities P1–P6)
- Exact file paths included in every task description

---

## Phase 1: Setup (Project Initialisation)

**Purpose**: Repository structure, uv project, database, test fixtures.
No user story work begins until this phase is complete.

- [ ] T001 Create full `src/` directory tree: `src/models/`, `src/aggregates/`, `src/commands/`, `src/projections/`, `src/upcasting/`, `src/integrity/`, `src/mcp/`, `src/what_if/`, `src/regulatory/`, `src/db/` — each with `__init__.py`
- [ ] T002 Create `tests/` directory with `tests/__init__.py` and stub files: `tests/test_concurrency.py`, `tests/test_upcasting.py`, `tests/test_projections.py`, `tests/test_gas_town.py`, `tests/test_mcp_lifecycle.py`
- [ ] T003 Initialise `pyproject.toml` with `uv`: Python 3.12+, deps `asyncpg>=0.29`, `pydantic>=2.0`, `mcp>=1.0`; dev deps `pytest>=8.0`, `pytest-asyncio>=0.23`, `ruff>=0.4`; `[tool.pytest.ini_options]` with `asyncio_mode = "auto"`
- [ ] T004 [P] Configure `ruff` in `pyproject.toml`: `line-length=100`, `target-version="py312"`, `select=["E","F","I","UP","ANN"]` — lint with `uv run ruff check src/ tests/`
- [ ] T005 [P] Create `.env.example`: `DATABASE_URL=postgresql://ledger:ledger_dev@localhost:5432/ledger`; add `.env` to `.gitignore`
- [ ] T006 Create `docker-compose.yml` at repo root: PostgreSQL 16-alpine service named `ledger-postgres`, port 5432, env `POSTGRES_DB=ledger`, `POSTGRES_USER=ledger`, `POSTGRES_PASSWORD=ledger_dev`
- [ ] T007 Create `tests/conftest.py`: async `asyncpg_pool` fixture that creates pool from `DATABASE_URL` env var; `event_loop` fixture; `unique_app_id()` helper returning `f"test-{uuid4()}"` for test isolation
- [ ] T008 [P] Create `scripts/` directory with stubs: `scripts/__init__.py`, `scripts/seed_demo_application.py` (stub), `scripts/query_history.py` (stub), `scripts/what_if_demo.py` (stub)

**Checkpoint**: `uv run pytest --collect-only` runs without import errors. Docker Postgres accessible.

---

## Phase 2: Foundational — Pre-Implementation Docs + Base Models

**Purpose**: Graded documents and base types that ALL phases and ALL user stories depend on.

**⚠️ CRITICAL**: Constitution Principle VI mandates `DOMAIN_NOTES.md` must be COMPLETE before
any implementation code. `DESIGN.md` section headings must exist before Phase 5 begins.
This phase BLOCKS all challenge phases.

- [ ] T009 Write `DOMAIN_NOTES.md` (graded independently) — answer all 6 domain analysis questions in full: (1) EDA vs ES distinction with LangChain callback redesign, (2) rejected aggregate boundary (ComplianceRecord merge considered and rejected — coupling analysis), (3) full concurrency trace (SELECT FOR UPDATE race walkthrough with winner/loser outcome), (4) projection lag consequence and UI staleness communication strategy, (5) CreditAnalysisCompleted v1→v2 upcaster with inference strategy justification, (6) Marten Async Daemon parallel in Python using PostgreSQL advisory lock leader election
- [ ] T010 [P] Create `DESIGN.md` with 7 section headings (content TBD in Phase 9): "1. Aggregate Boundary Justification", "2. Projection Strategy", "3. Concurrency Analysis", "4. Upcasting Inference Decisions", "5. EventStoreDB Comparison & Enterprise Stack Translation", "6. What I Would Do Differently", "7. Schema Column Justifications" — each with a `TODO` placeholder body
- [ ] T011 Create `src/schema.sql`: exact schema from challenge — `events`, `event_streams`, `projection_checkpoints`, `outbox` tables with all constraints and indexes; plus `compliance_snapshots` and `projection_errors` tables per `data-model.md` sections 1.5–1.6
- [ ] T012 Create `src/db/pool.py`: `create_pool(dsn: str) -> asyncpg.Pool` async factory; `run_migrations(pool: asyncpg.Pool, schema_path: Path) -> None` that executes `schema.sql`; `__main__` block for `python -m src.db.pool migrate`
- [ ] T013 Implement `src/models/events.py`: `BaseEvent(BaseModel, frozen=True)` with `ClassVar[str] event_type` and `ClassVar[int] event_version = 1`; `StoredEvent(BaseModel, frozen=True)` with all 9 fields from `data-model.md` section 2.1; `StreamMetadata`; `StoredEvent.with_payload()` returning new instance (immutable)
- [ ] T014 Implement `src/models/exceptions.py`: `OptimisticConcurrencyError` (stream_id, expected_version, actual_version, suggested_action); `ConcurrencyExhaustedError` (stream_id, attempts); `DomainError` (rule, message); `StreamNotFoundError` (stream_id); `PreconditionFailed` (precondition, suggested_action); `RateLimitedError` (entity_id, retry_after_seconds)
- [ ] T015 [P] Implement `src/models/commands.py`: all 7 Pydantic command models from `data-model.md` section 6 — `SubmitApplicationCommand`, `CreditAnalysisCompletedCommand`, `FraudScreeningCompletedCommand`, `GenerateDecisionCommand`, `RecordHumanReviewCommand`, `StartAgentSessionCommand`, `RecordComplianceCheckCommand`
- [ ] T016 [P] Implement `src/models/results.py`: `IntegrityCheckResult`, `AgentContext`, `WhatIfResult` from `data-model.md` section 7; `ApplicationState` and `SessionHealthStatus` enums from `data-model.md` section 4

**Checkpoint**: `uv run pytest --collect-only` still clean. `uv run ruff check src/` passes. DB migrates cleanly with `python -m src.db.pool migrate`.

---

## Phase 3: Challenge Phase 1 — Event Store Core

**Purpose**: Implement and test the `EventStore` interface per `contracts/event-store-interface.md`.
All 6 interface methods operational. OCC enforced. test_concurrency.py passes.
**User stories enabled**: US1 (audit trail reads), US2 (OCC for concurrency), US3 (stream creation)

**⚠️ GATE**: `tests/test_concurrency.py` MUST pass before Phase 4 begins.

### Event Store Implementation

- [ ] T017 [US1] [US2] [US3] Implement `src/event_store.py` — `EventStore.__init__`: accept `asyncpg.Pool` and `UpcasterRegistry` (type: `Any` temporarily until registry exists); define all 6 method signatures with `NotImplementedError`
- [ ] T018 [US1] [US2] [US3] Implement `EventStore.append()` in `src/event_store.py`: open transaction; `SELECT current_version FROM event_streams WHERE stream_id=$1 FOR UPDATE`; check expected_version (raise `OptimisticConcurrencyError` on mismatch; handle `-1` new-stream case); `INSERT INTO events` for each event with incrementing `stream_position`; `UPDATE event_streams`; `INSERT INTO outbox` for each event; commit atomically; return new version
- [ ] T019 [P] [US1] [US2] [US3] Implement `EventStore.load_stream()` in `src/event_store.py`: `SELECT * FROM events WHERE stream_id=$1 AND stream_position >= $2 [AND stream_position <= $3] ORDER BY stream_position`; apply `upcaster_registry.upcast(event)` to each loaded event before returning; raise `StreamNotFoundError` if stream not in `event_streams`
- [ ] T020 [P] [US1] Implement `EventStore.load_all()` in `src/event_store.py`: async generator; `SELECT * FROM events WHERE global_position > $1 [AND event_type = ANY($2)] ORDER BY global_position LIMIT $batch_size`; yield upcasted events; loop until no more rows
- [ ] T021 [P] [US1] [US2] [US3] Implement `EventStore.stream_version()`, `archive_stream()`, `get_stream_metadata()` in `src/event_store.py` per `contracts/event-store-interface.md` behaviour contracts

### Phase 1 Mandatory Test

- [ ] T022 [US1] [US2] Write `tests/test_concurrency.py` — **double-decision test**: seed a stream at version 3 (3 events); spawn two concurrent `asyncio.create_task` calls both appending with `expected_version=3`; assert: (a) total stream length = 4 (not 5), (b) winner's event has `stream_position=4`, (c) loser raises `OptimisticConcurrencyError` (not silently swallowed); test uses real asyncpg pool from conftest; application_id is unique per run
- [ ] T023 [US2] Run `tests/test_concurrency.py` and verify it passes — this is the Phase 1 → Phase 2 gate

**Checkpoint**: `uv run pytest tests/test_concurrency.py -v` passes. All 6 EventStore methods return correct results in manual integration smoke-test.

---

## Phase 4: Challenge Phase 2 — Domain Logic & Aggregates

**Purpose**: Four aggregates, state machines, all 6 business rules inside aggregate layer,
all 7 command handlers. Zero business logic in handlers.
**User stories enabled**: US3 (loan lifecycle), US2 (agent session), US1 (business rules enforce audit correctness)

**⚠️ GATE**: All 6 business rules tested before Phase 5 begins.

### LoanApplication Aggregate

- [ ] T024 [US3] Implement `src/aggregates/loan_application.py` — `LoanApplicationAggregate`: fields from `data-model.md` section 4.1; `VALID_TRANSITIONS` dict; `load()` classmethod replaying stream via `store.load_stream(f"loan-{application_id}")`; `_apply()` dispatcher routing to `_on_*` per event type; `self.version = event.stream_position` on every event
- [ ] T025 [US3] Implement all `_on_*` handlers in `src/aggregates/loan_application.py`: `_on_ApplicationSubmitted` (state=SUBMITTED, store applicant_id/amount), `_on_CreditAnalysisRequested` (state=AWAITING_ANALYSIS), `_on_AnalysisCompleted` (state=ANALYSIS_COMPLETE), `_on_DecisionGenerated` (state=PENDING_DECISION; enforce confidence floor BR-004: confidence_score<0.6 → assert recommendation=="REFER"), `_on_HumanReviewCompleted` (state=APPROVED/DECLINED_PENDING_HUMAN), `_on_ApplicationApproved` (state=FINAL_APPROVED), `_on_ApplicationDeclined` (state=FINAL_DECLINED), `_on_HumanReviewOverride` (unlock credit_analysis_locked)
- [ ] T026 [US3] [US1] Implement all assertion methods in `src/aggregates/loan_application.py`: `assert_state(expected)` (raises `DomainError(rule="invalid_state_transition")`); `assert_awaiting_credit_analysis()`; `assert_analysis_not_locked()` (BR-003: raises `DomainError(rule="credit_analysis_locked")`); `assert_compliance_cleared()` (BR-005: checks `compliance_checks_required` vs `compliance_checks_passed`); `assert_contributing_sessions_valid(session_ids)` (BR-006: verifies all session IDs are in `contributing_sessions`); `assert_can_approve()`

### AgentSession Aggregate

- [ ] T027 [P] [US2] Implement `src/aggregates/agent_session.py` — `AgentSessionAggregate`: fields from `data-model.md` section 4.2; `load()` loading `f"agent-{agent_id}-{session_id}"` stream; `_on_AgentContextLoaded` (set `context_loaded=True`, store `model_version`); `_on_CreditAnalysisCompleted` / `_on_FraudScreeningCompleted` / `_on_DecisionGenerated` (add to `completed_application_ids`); `assert_context_loaded()` (BR-002: raises `DomainError(rule="context_not_loaded")`); `assert_model_version_current(model_version)`

### ComplianceRecord Aggregate

- [ ] T028 [P] [US3] Implement `src/aggregates/compliance_record.py` — `ComplianceRecordAggregate`: fields from `data-model.md` section 4.3; `load()` loading `f"compliance-{application_id}"` stream (returns empty aggregate if stream not found — not an error); `_on_ComplianceCheckRequested`, `_on_ComplianceRulePassed`, `_on_ComplianceRuleFailed`, `_on_ComplianceClearanceGranted`; `is_cleared()` returns `set(checks_required) <= set(checks_passed)`; `assert_all_checks_passed()`; `assert_rule_id_valid(rule_id)`

### AuditLedger Aggregate

- [ ] T029 [P] [US1] Implement `src/aggregates/audit_ledger.py` — `AuditLedgerAggregate`: fields from `data-model.md` section 4.4; `load()` loading `f"audit-{entity_type}-{entity_id}"` stream; `_on_AuditIntegrityCheckRun` (update `last_integrity_hash`, `last_check_at`); `assert_rate_limit_not_exceeded()` (raises `RateLimitedError` if `last_check_at` was < 60s ago)

### Command Handlers

- [ ] T030 [US3] Implement `handle_submit_application()` in `src/commands/handlers.py`: validate `SubmitApplicationCommand`; call `store.append(f"loan-{cmd.application_id}", [ApplicationSubmitted(...)], expected_version=-1)` — `expected_version=-1` enforces uniqueness; return `stream_id, initial_version`
- [ ] T031 [US2] Implement `handle_start_agent_session()` in `src/commands/handlers.py`: load `AgentSessionAggregate`; if `context_loaded=True` already, return existing position; append `AgentContextLoaded` event with `expected_version=-1` for new session (or current version if resuming)
- [ ] T032 [US1] [US3] Implement `handle_credit_analysis_completed()` in `src/commands/handlers.py`: load `LoanApplicationAggregate` + `AgentSessionAggregate`; call `app.assert_awaiting_credit_analysis()`, `app.assert_analysis_not_locked()`, `agent.assert_context_loaded()`, `agent.assert_model_version_current()`; compute `input_data_hash = hashlib.sha256(canonical_json(cmd.input_data)).hexdigest()`; append `CreditAnalysisCompleted` event; update `credit_analysis_locked = True`
- [ ] T033 [P] [US3] Implement `handle_fraud_screening_completed()` in `src/commands/handlers.py`: load agent session; `agent.assert_context_loaded()`; validate `fraud_score` in `[0.0, 1.0]`; append `FraudScreeningCompleted` event to agent stream
- [ ] T034 [P] [US3] Implement `handle_compliance_check()` in `src/commands/handlers.py`: load `ComplianceRecordAggregate`; `compliance.assert_rule_id_valid(cmd.rule_id)`; append `ComplianceRulePassed` or `ComplianceRuleFailed` based on `cmd.passed`; if `compliance.is_cleared()` after append, also append `ComplianceClearanceGranted`
- [ ] T035 [US1] Implement `handle_generate_decision()` in `src/commands/handlers.py`: load `LoanApplicationAggregate`; `app.assert_state(PENDING_DECISION)` (or COMPLIANCE_REVIEW); `app.assert_contributing_sessions_valid(cmd.contributing_agent_sessions)`; load each session and verify it has a decision event for this `application_id` (BR-006 causal chain); append `DecisionGenerated` — note: confidence floor BR-004 enforced in aggregate `_on_DecisionGenerated` handler
- [ ] T036 [US3] Implement `handle_human_review_completed()` in `src/commands/handlers.py`: load `LoanApplicationAggregate`; assert state is APPROVED/DECLINED_PENDING_HUMAN; if `cmd.override and not cmd.override_reason`: raise `DomainError(rule="override_reason_required")`; append `HumanReviewCompleted`; append `ApplicationApproved` or `ApplicationDeclined` based on `cmd.final_decision`

### Domain Business Rule Tests

- [ ] T037 [US1] [US2] [US3] Write `tests/test_domain.py` — test all 6 business rules with invalid inputs, asserting correct `DomainError.rule` value: (a) `"invalid_state_transition"` on out-of-order state; (b) `"context_not_loaded"` on agent decision without session; (c) `"credit_analysis_locked"` on second credit analysis; (d) `"confidence_floor_violated"` on APPROVE with confidence < 0.6; (e) `"compliance_not_cleared"` on approval without all checks passed; (f) `"invalid_causal_chain"` on decision referencing sessions that never processed this application
- [ ] T038 [US1] [US2] [US3] Run `tests/test_domain.py` — all 6 business rule tests must pass (Phase 2 → Phase 3 gate)

**Checkpoint**: `uv run pytest tests/test_concurrency.py tests/test_domain.py -v` all pass.

---

## Phase 5: Challenge Phase 3 — Projections & Async Daemon

**Purpose**: Three CQRS read models + fault-tolerant daemon + lag SLO enforcement.
test_projections.py passes.
**User stories enabled**: US4 (temporal compliance), US5 (health/lag monitoring), US1 (audit trail reads from projections)

**⚠️ GATE**: `tests/test_projections.py` MUST pass before Phase 6 begins.

### Projection Daemon

- [ ] T039 Implement `src/projections/daemon.py` — `ProjectionDaemon.__init__`: accept `EventStore` and `list[Projection]`; store `_projections` dict; `_running` flag; in-memory lag cache `_lags: dict[str, int]`
- [ ] T040 Implement `ProjectionDaemon.run_forever()` and `_process_batch()` in `src/projections/daemon.py`: `run_forever(poll_interval_ms=100)` loops calling `_process_batch()` then `asyncio.sleep`; `_process_batch()` loads lowest checkpoint across projections; calls `store.load_all(from_global_position)` in batches; routes each event to all subscribed projections inside `try/except`; on handler failure: increment retry counter; after 3 failures: insert into `projection_errors`, skip, continue; update `projection_checkpoints` after each successful batch; update `_lags[name] = now_ms - event.recorded_at_ms`
- [ ] T041 [P] [US5] Implement `ProjectionDaemon.get_lag(projection_name: str) -> int` and `get_all_lags() -> dict[str, int]` in `src/projections/daemon.py`: return from in-memory `_lags` cache (no DB call — required for p99 < 10 ms health endpoint); add SLO breach detection logging: `CRITICAL` when lag > 3× SLO for 5 consecutive cycles

### ApplicationSummary Projection

- [ ] T042 [US1] Implement `src/projections/application_summary.py` — `ApplicationSummaryProjection`: `name = "application_summary"`; `SLO_MS = 500`; `subscribed_event_types` list; `handle(event, conn)` method: UPSERT into `application_summary` table for each handled event type (`ApplicationSubmitted`, `CreditAnalysisRequested`, `CreditAnalysisCompleted`, `FraudScreeningCompleted`, `DecisionGenerated`, `HumanReviewCompleted`, `ApplicationApproved`, `ApplicationDeclined`)
- [ ] T043 [P] Implement `src/projections/application_summary.py` — `build_application_summary_table()` DDL helper ensuring `application_summary` table exists (create if not exists); include in schema.sql as well

### AgentPerformanceLedger Projection

- [ ] T044 [P] [US5] Implement `src/projections/agent_performance.py` — `AgentPerformanceLedger` projection: `name = "agent_performance_ledger"`; handles `CreditAnalysisCompleted`, `FraudScreeningCompleted`, `DecisionGenerated`, `HumanReviewCompleted`; UPSERT with running average calculation for `avg_confidence_score`, `avg_duration_ms`; increment `analyses_completed`, `decisions_generated`; update `approve_rate`, `decline_rate`, `refer_rate`, `human_override_rate` as running fractions

### ComplianceAuditView Projection

- [ ] T045 [US4] Implement `src/projections/compliance_audit.py` — `ComplianceAuditViewProjection`: `name = "compliance_audit_view"`; `SLO_MS = 2000`; handles `ComplianceCheckRequested`, `ComplianceRulePassed`, `ComplianceRuleFailed`, `ComplianceClearanceGranted`; UPSERT into `compliance_audit_view` table
- [ ] T046 [US4] Implement `ComplianceAuditViewProjection.get_compliance_at(application_id, timestamp)` in `src/projections/compliance_audit.py`: (1) find nearest preceding snapshot from `compliance_snapshots WHERE application_id=$1 AND snapshot_at <= $2 ORDER BY snapshot_at DESC LIMIT 1`; (2) if no snapshot, replay from position 0; (3) load compliance events from `snapshot_position` to `recorded_at <= timestamp`; (4) apply events to snapshot state; return state
- [ ] T047 [P] [US4] Implement `ComplianceAuditViewProjection.rebuild_from_scratch()` in `src/projections/compliance_audit.py`: truncate `compliance_audit_view` and `compliance_snapshots` tables; reset projection checkpoint to 0; read lock NOT held during rebuild — live reads served from table as rebuild progresses (no downtime to live reads)
- [ ] T048 [P] [US4] Implement snapshot trigger logic in `src/projections/compliance_audit.py`: after every successful `ComplianceRulePassed` or `ComplianceRuleFailed` event handled, check if `events_since_last_snapshot >= 100` for this `application_id`; if yes, INSERT snapshot into `compliance_snapshots`

### Phase 3 Mandatory Test

- [ ] T049 [US4] [US5] Write `tests/test_projections.py` — **SLO lag tests**: (1) seed 50 concurrent command handler coroutines each submitting a complete application workflow; start ProjectionDaemon; wait until all events processed; assert `daemon.get_lag("application_summary") < 500`; assert `daemon.get_lag("compliance_audit_view") < 2000`; (2) **rebuild_from_scratch test**: confirm projection has data; call `rebuild_from_scratch()`; confirm projection repopulated from zero; confirm live reads were served during rebuild (non-blocking); (3) **temporal query test**: record compliance events at T1 and T3; query `get_compliance_at(id, T2)`; assert returns only T1 state
- [ ] T050 [US4] [US5] Run `tests/test_projections.py` and verify all assertions pass (Phase 3 → Phase 4 gate)

**Checkpoint**: `uv run pytest tests/test_concurrency.py tests/test_domain.py tests/test_projections.py -v` all pass.

---

## Phase 6: Challenge Phase 4 — Upcasting, Integrity & Gas Town

**Purpose**: Automatic version migration; SHA-256 audit chain; agent crash recovery.
test_upcasting.py and test_gas_town.py pass.
**User stories enabled**: US1 (upcasting + integrity for complete audit trail), US2 (Gas Town recovery), US5 (integrity checks)

**⚠️ GATE**: `tests/test_upcasting.py` AND `tests/test_gas_town.py` MUST pass before Phase 7.

### UpcasterRegistry

- [ ] T051 Implement `src/upcasting/registry.py` — `UpcasterRegistry`: `__init__` with empty `_upcasters: dict[tuple[str, int], Callable]`; `register(event_type, from_version)` decorator storing fn at `(event_type, from_version)` key; `upcast(event: StoredEvent) -> StoredEvent` chain traversal: while `(event_type, v)` in `_upcasters`, call upcaster, create new `StoredEvent` via `event.with_payload(new_payload, version=v+1)`, increment v; NEVER mutate input event; module-level singleton: `registry = UpcasterRegistry()`
- [ ] T052 Wire `UpcasterRegistry` into `EventStore` in `src/event_store.py`: replace `Any` type hint with actual import; confirm `load_stream()` and `load_all()` call `registry.upcast(event)` before returning each event

### Upcasters

- [ ] T053 [US1] Implement `CreditAnalysisCompleted` v1→v2 upcaster in `src/upcasting/upcasters.py`: `@registry.register("CreditAnalysisCompleted", from_version=1)` decorator; add `model_version` inferred from `recorded_at` via deployment timeline lookup (events before 2026-01-01 → `"legacy-pre-2026"`); add `confidence_score = None` (REGULATORY DECISION: null because fabricating a score would create false precision in audit records — documented in DESIGN.md Section 4); add `regulatory_basis` inferred from regulation version active at `recorded_at`
- [ ] T054 [P] [US1] Implement `DecisionGenerated` v1→v2 upcaster in `src/upcasting/upcasters.py`: `@registry.register("DecisionGenerated", from_version=1)` decorator; reconstruct `model_versions{}` dict by loading each contributing session's `AgentContextLoaded` event (store lookup inside upcaster — O(n) where n=contributing sessions, typically 3–4; PERFORMANCE NOTE documented in DESIGN.md Section 4)

### Phase 4A Mandatory Test

- [ ] T055 [US1] Write `tests/test_upcasting.py` — **immutability test**: (1) append a `CreditAnalysisCompleted` event with `event_version=1` directly (simulating legacy data); (2) query raw DB: `SELECT payload FROM events WHERE event_id=$id` — store as `raw_before`; (3) call `store.load_stream()` and assert returned event has `event_version=2` with `model_version` and `confidence_score=None` fields present; (4) query raw DB again — assert `payload == raw_before` byte-for-byte; this test MUST detect any upcaster that writes back to the database
- [ ] T056 [US1] Run `tests/test_upcasting.py` and verify passes

### Cryptographic Audit Chain

- [ ] T057 [US1] [US5] Implement `src/integrity/audit_chain.py` — `run_integrity_check(store, entity_type, entity_id) -> IntegrityCheckResult`: (1) load all events for entity's primary stream; (2) load last `AuditIntegrityCheckRun` event from `audit-{entity_type}-{entity_id}` stream (or use genesis hash `"0" * 64` if none); (3) compute `event_hash = sha256(json.dumps(event.payload, sort_keys=True, ensure_ascii=False))` for each event; (4) build chain: `chain_hash = sha256(previous_hash.encode() + event_hash.encode())`; (5) compare to stored `integrity_hash` if exists — set `chain_valid` and `tamper_detected`; (6) load `AuditLedgerAggregate` and call `assert_rate_limit_not_exceeded()`; (7) append new `AuditIntegrityCheckRun` event; return `IntegrityCheckResult`
- [ ] T058 [P] [US1] [US5] Implement `src/integrity/audit_chain.py` — helpers: `canonical_json(payload: dict) -> str`; `sha256_hex(data: str) -> str`; `GENESIS_HASH = "0" * 64`

### Gas Town Agent Memory Pattern

- [ ] T059 [US2] Implement `src/integrity/gas_town.py` — `reconstruct_agent_context(store, agent_id, session_id, token_budget=8000) -> AgentContext`: (1) load full `AgentSession` stream; (2) identify "preserve verbatim" events: last 3 events + any event whose type ends in `"Requested"` without a corresponding completion event for same `application_id` (NEEDS_RECONCILIATION detection); (3) summarise older events as one prose sentence per event type using event payload fields; (4) count tokens in verbatim section (estimate: 4 chars/token); (5) fill remaining budget with prose summary, truncating oldest first; (6) set `session_health_status = NEEDS_RECONCILIATION` if any partial decision detected; return `AgentContext`
- [ ] T060 [P] [US2] Implement NEEDS_RECONCILIATION detection helper in `src/integrity/gas_town.py`: an event is "partial" if it is a `*Requested` type with no corresponding `*Completed` event for the same `application_id` in the same session stream; return list of `pending_work` items describing each unresolved action

### Phase 4C Mandatory Test

- [ ] T061 [US2] Write `tests/test_gas_town.py` — **simulated crash recovery**: (1) start an agent session (append `AgentContextLoaded`); (2) append 4 more events including a `CreditAnalysisRequested` with no corresponding `CreditAnalysisCompleted` (partial decision); (3) discard the in-memory agent object (no reference retained); (4) call `reconstruct_agent_context(store, agent_id, session_id, token_budget=2000)`; (5) assert: `context.context_text` is non-empty; `context.session_health_status == NEEDS_RECONCILIATION`; `context.pending_work` contains at least one item; `context.last_event_position == 5`; context was reconstructed WITHOUT any in-memory agent state
- [ ] T062 [US2] Run `tests/test_gas_town.py` and verify passes (Phase 4 → Phase 5 gate)

**Checkpoint**: `uv run pytest tests/test_concurrency.py tests/test_domain.py tests/test_projections.py tests/test_upcasting.py tests/test_gas_town.py -v` — all four mandatory tests pass.

---

## Phase 7: Challenge Phase 5 — MCP Server

**Purpose**: 8 typed tools (command side) + 6 resources (query side). Full lifecycle
integration test via MCP only — no direct Python function calls.
**User stories enabled**: US1 + US2 + US3 + US4 + US5 (all via MCP interface)

**⚠️ GATE**: `tests/test_mcp_lifecycle.py` MUST pass. This is the final mandatory test gate.

### MCP Server Entry Point

- [ ] T063 Implement `src/mcp/server.py`: `FastMCP("The Ledger")` instance; lifespan context manager that creates `asyncpg.Pool` from `DATABASE_URL`, runs migrations, initialises `EventStore`, registers `UpcasterRegistry` upcasters, creates and starts `ProjectionDaemon` as `asyncio.Task`; shut down daemon task on server stop; expose FastMCP app as `mcp` for `python -m src.mcp.server` entrypoint

### MCP Tools (Command Side)

- [ ] T064 [US3] Implement `submit_application` tool in `src/mcp/tools.py`: `@mcp.tool()` decorator; tool description includes "always first tool in loan lifecycle"; validate `SubmitApplicationCommand`; call `handle_submit_application()`; catch `OptimisticConcurrencyError` → return structured error JSON per `contracts/mcp-tools.md`; return `{"stream_id": ..., "initial_version": ...}`
- [ ] T065 [US2] Implement `start_agent_session` tool in `src/mcp/tools.py`: description states "MUST be called before record_credit_analysis, record_fraud_screening, or generate_decision — calling without active session returns PreconditionFailed"; call `handle_start_agent_session()`; return `{"session_id": ..., "context_position": ...}`
- [ ] T066 [US2] [US1] Implement `record_credit_analysis` tool in `src/mcp/tools.py`: description states precondition (active AgentSession); call `handle_credit_analysis_completed()`; catch `OptimisticConcurrencyError` → retry up to 3× with exponential backoff (100/200/400 ms); after 3 failures raise `ConcurrencyExhaustedError`; all errors return structured JSON per contracts
- [ ] T067 [P] [US2] [US1] Implement `record_fraud_screening` tool in `src/mcp/tools.py`: validate `fraud_score` in [0.0, 1.0] — return `ValidationError` structured response on violation; call `handle_fraud_screening_completed()`
- [ ] T068 [P] [US4] Implement `record_compliance_check` tool in `src/mcp/tools.py`: validate rule_id against application's compliance record; call `handle_compliance_check()`; return `{"check_id": ..., "compliance_status": ...}`
- [ ] T069 [US1] Implement `generate_decision` tool in `src/mcp/tools.py`: description states confidence floor enforcement and causal chain requirement; call `handle_generate_decision()`; note confidence floor is enforced in aggregate, not here — tool description explains the override behaviour for LLM consumers; return `{"decision_id": ..., "recommendation": ...}`
- [ ] T070 [P] [US3] Implement `record_human_review` tool in `src/mcp/tools.py`: caller-supplied `reviewer_id` stored verbatim (no credential validation per FR-009); call `handle_human_review_completed()`; return `{"final_decision": ..., "application_state": ...}`
- [ ] T071 [P] [US5] Implement `run_integrity_check` tool in `src/mcp/tools.py`: description states rate limit (1/minute per entity) and compliance role expectation; call `run_integrity_check()`; catch `RateLimitedError` → return structured rate limit response; return full `IntegrityCheckResult`

### MCP Resources (Query Side)

- [ ] T072 [US1] Implement `ledger://applications/{id}` resource in `src/mcp/resources.py`: `@mcp.resource("ledger://applications/{id}")` decorator; read from `application_summary` table (NO `store.load_stream()` call); include `_meta.projection_lag_ms` from `daemon.get_lag("application_summary")`; p99 < 50 ms — single indexed lookup
- [ ] T073 [P] [US4] Implement `ledger://applications/{id}/compliance` resource in `src/mcp/resources.py`: parse optional `?as_of=` query parameter; if present call `projection.get_compliance_at(id, timestamp)`; if absent read current `compliance_audit_view` row; include `_meta.snapshot_used` flag; p99 < 200 ms
- [ ] T074 [P] [US1] Implement `ledger://applications/{id}/audit-trail` resource in `src/mcp/resources.py`: justified exception — call `store.load_stream()` for `audit-{entity_type}-{entity_id}`; parse `?from=` and `?to=` range parameters; include last integrity check status; p99 < 500 ms
- [ ] T075 [P] [US5] Implement `ledger://agents/{id}/performance` resource in `src/mcp/resources.py`: read from `agent_performance_ledger` table by `agent_id`; return all model_version rows; p99 < 50 ms
- [ ] T076 [P] [US2] Implement `ledger://agents/{id}/sessions/{session_id}` resource in `src/mcp/resources.py`: justified exception — call `store.load_stream(f"agent-{id}-{session_id}")`; include `health_status` and `context_loaded` from aggregate replay; p99 < 300 ms
- [ ] T077 [US5] Implement `ledger://ledger/health` resource in `src/mcp/resources.py`: call `daemon.get_all_lags()` (in-memory — NO DB call); compute `status` = healthy/degraded/critical per logic in `contracts/mcp-resources.md`; p99 < 10 ms

### Phase 5 Mandatory Test

- [ ] T078 [US1] [US2] [US3] [US4] [US5] Write `tests/test_mcp_lifecycle.py` — **full lifecycle via MCP only** (no direct Python function calls): initialise test MCP server with real DB; call `start_agent_session` × 1; call `submit_application`; call `record_credit_analysis`; call `record_fraud_screening`; call `record_compliance_check` × N (all required rules); call `generate_decision`; call `record_human_review`; query `ledger://applications/{id}/compliance` and assert complete compliance trace present; query `ledger://applications/{id}` and assert state = `FinalApproved` or `FinalDeclined`; assert zero direct handler/store calls were made
- [ ] T079 [US1] [US2] [US3] [US4] [US5] Run `tests/test_mcp_lifecycle.py` and verify passes — this is the Phase 5 → Phase 6 / Polish gate

**Checkpoint**: ALL 5 mandatory tests pass: `uv run pytest tests/ -v`

---

## Phase 8: Challenge Phase 6 — Bonus (Score 5)

**Purpose**: What-if counterfactual projector + regulatory examination package.
**User story enabled**: US6 (P6 — Bonus, required for Score 5)
**Prerequisite**: Phases 1–5 fully complete with all 5 mandatory tests passing.

- [ ] T080 [US6] Implement `src/what_if/projector.py` — `run_what_if(store, application_id, branch_at_event_type, counterfactual_events, projections) -> WhatIfResult`: (1) load all events for application stream up to the branch point event type; (2) identify the branch event and all events AFTER it; (3) determine causally dependent events: those whose `causation_id` traces back to the branch event or any event after it; (4) construct counterfactual sequence: pre-branch real events + `counterfactual_events` + causally INDEPENDENT post-branch events; (5) apply combined sequence to each projection in-memory; (6) return `WhatIfResult(real_outcome, counterfactual_outcome, divergence_events)`; NEVER call `store.append()` — zero writes to real store
- [ ] T081 [P] [US6] Implement causal dependency tracer in `src/what_if/projector.py` — `is_causally_dependent(event, branch_event_ids: set[str]) -> bool`: check `event.metadata.get("causation_id")` against `branch_event_ids`; recursively build the causal closure if needed (walk causation chain)
- [ ] T082 [P] [US6] Implement `src/regulatory/package.py` — `generate_regulatory_package(store, application_id, examination_date) -> dict`: (1) load complete event stream for application; (2) load all projection states as they existed at `examination_date` using temporal queries; (3) run hash chain integrity check; (4) generate plain-English narrative: one sentence per significant event using event payload fields; (5) extract AI model metadata from all `AgentContextLoaded` events in contributing sessions; (6) return self-contained JSON dict independently verifiable against raw DB data
- [ ] T083 [US6] Test what-if scenario in `tests/test_what_if.py`: (1) complete full lifecycle with `risk_tier="MEDIUM"`, final decision `APPROVE`; (2) run `run_what_if()` substituting `risk_tier="HIGH"` at `CreditAnalysisCompleted` branch; (3) assert `real_outcome.decision == "APPROVE"` and `counterfactual_outcome.decision != "APPROVE"` (materially different due to business rule cascade); (4) assert `divergence_events` is non-empty; (5) assert raw DB event count unchanged before and after (zero writes to store)
- [ ] T084 [P] [US6] Test regulatory package in `tests/test_regulatory.py`: generate package for completed application; verify JSON is self-contained (no foreign key lookups required); verify narrative contains one sentence per significant event; verify integrity verification result is present; verify model metadata includes confidence scores and input_data_hashes for all contributing agents

---

## Phase 9: Polish & Cross-Cutting Concerns

**Purpose**: Complete architecture docs, README, demo scripts, Week Standard timing,
final lint pass. No new features.

- [ ] T085 Complete `DESIGN.md` Section 1 (Aggregate Boundary Justification): explain why `ComplianceRecord` is a separate aggregate; trace the coupling problem that merging would create (concurrent writes from N compliance agents vs single loan state machine lock); quantify write serialisation cost under 100 concurrent applications × 4 agents
- [ ] T086 [P] Complete `DESIGN.md` Section 2 (Projection Strategy): for each projection justify Inline vs Async choice and SLO commitment; for `ComplianceAuditView` justify event-count snapshot trigger (100 events), snapshot invalidation policy (never invalidated — append-only; rebuild truncates)
- [ ] T087 [P] Complete `DESIGN.md` Section 3 (Concurrency Analysis): calculate expected `OptimisticConcurrencyError` rate at 100 concurrent applications × 4 agents; state retry strategy (3 retries, 100/200/400 ms backoff); state max retry budget and failure escalation policy; estimate probability of `ConcurrencyExhaustedError` per 1,000 operations
- [ ] T088 [P] Complete `DESIGN.md` Section 4 (Upcasting Inference Decisions): for each inferred field, quantify likely error rate and downstream consequence of incorrect inference; explain null vs fabrication decision for `confidence_score`; document O(n) store lookup in `DecisionGenerated` v1→v2 and when it would become a performance problem
- [ ] T089 [P] Complete `DESIGN.md` Section 5 (EventStoreDB Comparison + Enterprise Stack Translation): fill in both mapping tables from `research.md` R-007 (PostgreSQL → EventStoreDB; PostgreSQL → Marten 7.x + Wolverine); include "advising a .NET client" paragraph and "advising EventStoreDB migration" paragraph
- [ ] T090 [P] Complete `DESIGN.md` Section 6 (What I Would Do Differently): name the single most significant architectural decision to reconsider with another full day; show genuine reflection distinguishing "what was built" from "what the best version would be"
- [ ] T091 Complete `DESIGN.md` Section 7 (Schema Column Justifications): justify every column in all 6 tables; identify any column that cannot be justified and explain why it was excluded
- [ ] T092 Write `README.md`: install (`uv sync`), DB setup (docker-compose), migrations (`python -m src.db.pool migrate`), run all tests (`uv run pytest tests/ -v`), start MCP server (`python -m src.mcp.server`), Week Standard demo commands, link to quickstart.md
- [ ] T093 Implement `scripts/seed_demo_application.py`: full lifecycle seeder via MCP tool calls (not direct function calls) — `start_agent_session` × 3 agents, `submit_application`, `record_credit_analysis`, `record_fraud_screening`, `record_compliance_check` × 3 rules, `generate_decision`, `record_human_review`; print each step with timing
- [ ] T094 [P] Implement `scripts/query_history.py`: call `ledger://applications/{id}/audit-trail` resource; format and print full event timeline; add `--compliance-only` flag for `ledger://applications/{id}/compliance`; add `--as-of` flag for temporal queries; print `_meta.projection_lag_ms`; time the total query and print elapsed
- [ ] T095 [P] Implement `scripts/what_if_demo.py` (Phase 6 bonus): run `run_what_if()` for a seeded application substituting `risk_tier="HIGH"`; print real outcome vs counterfactual; print divergence events
- [ ] T096 Run Week Standard demo end-to-end: `time uv run python scripts/seed_demo_application.py --application-id demo-001` + `time uv run python scripts/query_history.py --application-id demo-001`; verify combined time < 60 seconds (SC-001 gate)
- [ ] T097 Run full mandatory test suite: `uv run pytest tests/ -v`; fix any regressions; all 5 mandatory test files must pass
- [ ] T098 [P] Run `uv run ruff check src/ tests/`; fix all linting errors; add type annotations where flagged by `ANN` rules

---

## Dependencies & Execution Order

### Phase Dependencies

```
Phase 1 (Setup)
  └─→ Phase 2 (Foundational Docs + Base Models)      ← DOMAIN_NOTES.md gate
        └─→ Phase 3 (Event Store Core)               ← test_concurrency.py gate
              └─→ Phase 4 (Domain Logic)             ← business rule tests gate
                    └─→ Phase 5 (Projections)         ← test_projections.py gate
                          └─→ Phase 6 (Upcasting/Integrity/Gas Town)
                          │         ← test_upcasting.py + test_gas_town.py gate
                          └─→ Phase 7 (MCP Server)   ← test_mcp_lifecycle.py gate
                                └─→ Phase 8 (Bonus)  ← optional; Score 5 only
                                └─→ Phase 9 (Polish) ← DESIGN.md + README + demo
```

### User Story ↔ Phase Mapping

| User Story | Primary Phases | Gate Test |
|------------|---------------|-----------|
| US1 — Regulatory Examiner / Week Standard | 3 (store) + 4 (domain) + 5 (projections) + 6 (integrity) + 7 (MCP) | test_mcp_lifecycle.py + SC-001 demo timing |
| US2 — AI Agent / Gas Town | 3 (OCC) + 4 (AgentSession) + 6 (gas_town.py) + 7 (MCP tools) | test_gas_town.py |
| US3 — Loan Officer | 4 (LoanApplication + handlers) + 7 (submit/review tools) | test_mcp_lifecycle.py |
| US4 — Compliance Officer / Temporal Query | 5 (ComplianceAuditView + snapshots) + 7 (compliance resource) | test_projections.py |
| US5 — System Admin / Integrity | 5 (daemon + health) + 6 (audit_chain.py) + 7 (health resource + integrity tool) | test_projections.py |
| US6 — Analyst / What-If (Bonus) | 8 (what_if/ + regulatory/) | test_what_if.py |

### Within Each Phase

- Base models (`events.py`, `exceptions.py`) before any aggregate or event store implementation
- `EventStore.append()` before any aggregate (aggregates call `store.append`)
- Aggregates before command handlers (handlers call `aggregate.load()`)
- Command handlers before MCP tools (tools call handlers)
- ProjectionDaemon before MCP resources (resources call `daemon.get_lag()`)
- `UpcasterRegistry.upcast()` wired into `EventStore.load_stream()` before upcasters are registered

### Parallel Opportunities

- T004, T005, T006, T007, T008 (Phase 1 setup) — all parallel
- T010, T011, T013, T014, T015, T016 (Phase 2 foundational) — all parallel after T009
- T019, T020, T021 (Phase 3 EventStore secondary methods) — parallel after T018
- T027, T028, T029 (Phase 4 AgentSession, ComplianceRecord, AuditLedger aggregates) — parallel after T024
- T033, T034 (Phase 4 fraud screening + compliance handlers) — parallel after T030
- T041, T043, T044 (Phase 5 daemon get_lag + ApplicationSummary DDL + AgentPerformance) — parallel
- T046, T047, T048 (Phase 5 compliance projection helpers) — parallel after T045
- T053, T054 (Phase 6 two upcasters) — parallel after T051+T052
- T057, T058 (Phase 6 audit chain + helpers) — parallel
- T064–T077 (Phase 7 MCP tools and resources) — mostly parallel per resource/tool, after T063
- T085–T091 (Phase 9 DESIGN.md sections) — all parallel

---

## Parallel Examples

### Phase 3 — Event Store Core

```bash
# After T017+T018 (append) complete, launch in parallel:
Task T019: load_stream() in src/event_store.py
Task T020: load_all() async generator in src/event_store.py
Task T021: stream_version(), archive_stream(), get_stream_metadata()
```

### Phase 4 — Aggregates

```bash
# All four aggregate files are independent — launch together:
Task T024: src/aggregates/loan_application.py (LoanApplicationAggregate)
Task T027: src/aggregates/agent_session.py (AgentSessionAggregate)
Task T028: src/aggregates/compliance_record.py (ComplianceRecordAggregate)
Task T029: src/aggregates/audit_ledger.py (AuditLedgerAggregate)
```

### Phase 7 — MCP Tools

```bash
# All tools are independent handler wrappers — launch together:
Task T067: record_fraud_screening tool in src/mcp/tools.py
Task T068: record_compliance_check tool in src/mcp/tools.py
Task T070: record_human_review tool in src/mcp/tools.py
Task T071: run_integrity_check tool in src/mcp/tools.py
```

---

## Implementation Strategy

### MVP First (Score 3 — US1+US2+US3 via Phases 1–5 + Phase 7)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (DOMAIN_NOTES.md + schema + base models)
3. Complete Phase 3: Event Store Core → **GATE**: test_concurrency.py passes
4. Complete Phase 4: Domain Logic → **GATE**: all 6 business rules tested
5. Complete Phase 5: Projections → **GATE**: test_projections.py passes
6. Complete Phase 6: Upcasting + Gas Town → **GATE**: test_upcasting.py + test_gas_town.py pass
7. Complete Phase 7: MCP Server → **GATE**: test_mcp_lifecycle.py passes
8. **STOP and VALIDATE**: Run Week Standard demo — must complete in < 60 seconds (SC-001)

### Score 4 — Add Temporal Compliance + Full DESIGN.md

1. Confirm ComplianceAuditView `get_compliance_at()` works under test_projections.py
2. Complete DESIGN.md Sections 1–4 with quantitative analysis
3. Verify `ledger://applications/{id}/compliance?as_of=` resource works

### Score 5 — Phase 8 Bonus + Full DESIGN.md Section 5–6

1. Complete Phase 8: What-if projector + regulatory package
2. Complete DESIGN.md Sections 5–6 (Enterprise Stack Translation + reflection)
3. Run Phase 9 Polish → demo all 6 video steps

---

## Notes

- **[P]** = task modifies a different file than its siblings; no blocking dependency
- **[USN]** label maps task to user story from `spec.md` for traceability
- Multiple `[US]` tags on one task = shared infrastructure serving multiple stories
- NEVER skip phase gates — Constitution Principle VIII is non-negotiable
- All tests use real PostgreSQL — no mocking of database layer
- After each phase gate passes: `uv run ruff check src/ tests/` before moving forward
- Commit after each gate checkpoint: `git commit -m "Phase N complete: [gate test] passes"`
