<!--
SYNC IMPACT REPORT
==================
Version change: (none — initial constitution) → 1.0.0
Bump rationale: MAJOR — first ratification; full project constitution established from
  challenge document TRP1 Week 5: Agentic Event Store & Enterprise Audit Infrastructure.

Modified principles: N/A (initial ratification)

Added sections:
  - Core Principles (9 principles)
  - Allowed Technology Stack
  - Deliverable Specification (Interim + Final)
  - Development Workflow & Phase Gates
  - Governance

Removed sections: N/A

Templates requiring updates:
  ✅ .specify/templates/constitution-template.md — constitution filled from this template
  ⚠  .specify/templates/plan-template.md — Constitution Check gates now defined;
        update "Language/Version" to Python 3.12+, "Storage" to PostgreSQL,
        "Testing" to pytest + asyncio, "Performance Goals" to match SLOs.
  ⚠  .specify/templates/spec-template.md — Success Criteria section must reference
        quantitative SLOs defined in Principle II (p99 < 50ms resources, lag < 500ms
        ApplicationSummary, lag < 2s ComplianceAuditView).
  ⚠  .specify/templates/tasks-template.md — Phase categorization must mirror the six
        challenge phases (0 Domain Recon → 1 Event Store → 2 Domain Logic →
        3 Projections → 4 Upcasting/Integrity → 5 MCP); task types must include
        observability (SLO measurement), integrity (hash-chain), and upcasting.

Follow-up TODOs (deferred items):
  - DESIGN.md: to be produced during Phase 1 implementation; six mandatory sections
    must be populated with quantitative analysis before Phase 2 begins.
  - DOMAIN_NOTES.md: graded deliverable; must be completed before any implementation
    code is written (challenge requirement).
  - README.md: must document install, migration, test-suite execution, and
    MCP server startup before Interim submission.
-->

# The Ledger Constitution
<!-- TRP1 Week 5: Agentic Event Store & Enterprise Audit Infrastructure -->

## Core Principles

### I. Challenge Document Fidelity (NON-NEGOTIABLE)

Every table, interface, schema, aggregate, event type, phase requirement, test
specification, and deliverable defined in the official challenge document
**TRP1 Week 5: Agentic Event Store & Enterprise Audit Infrastructure** is
binding law for this project. Nothing may be omitted, substituted, or
"improved" away from the documented specification without explicit written
justification in `DESIGN.md`.

**Specific bindings:**
- The four-table PostgreSQL schema (`events`, `event_streams`,
  `projection_checkpoints`, `outbox`) MUST be implemented exactly as specified,
  including all indexes and the `UNIQUE (stream_id, stream_position)` constraint.
- The thirteen-event catalogue (plus any missing events identified during Phase 0)
  MUST be implemented with the exact payload fields listed in the challenge.
- The four aggregates (`LoanApplication`, `AgentSession`, `ComplianceRecord`,
  `AuditLedger`) with their stream ID formats MUST be implemented as specified.
- The six business invariants defined in Phase 2 MUST all be enforced; none may
  be deferred as "nice to have."
- The six `DESIGN.md` sections and the five `DOMAIN_NOTES.md` analysis questions
  MUST be answered with specificity before implementation begins.
- `DOMAIN_NOTES.md` MUST be produced before any implementation code is written.

**Rationale:** The challenge is assessed against the specification verbatim.
Deviations — even well-intentioned improvements — are grading failures.

### II. Production-Grade Code Standards

All implementation code MUST meet the standard of work that would be deployed
to a live enterprise client engagement on Day 1 of a field deployment.

**Non-negotiable requirements:**
- **Async-first:** All I/O operations MUST use `async`/`await`; no blocking
  calls inside the async event loop.
- **Type-safe:** Every function signature MUST carry full type annotations
  (Python 3.12+ syntax); Pydantic models MUST be used for all event payloads
  and command objects. `Any` is forbidden except in explicitly justified
  upcaster internals.
- **Fault-tolerant:** The `ProjectionDaemon` MUST NOT crash on a bad event.
  It MUST log, skip (after configurable retry count), and continue. A daemon
  that halts on a single bad event is a production incident.
- **SLO-aware:** Every component that the challenge assigns an SLO to MUST
  enforce and measure that SLO in the test suite:
  - `ledger://applications/{id}` resource: p99 < 50 ms
  - `ledger://agents/{id}/performance` resource: p99 < 50 ms
  - `ledger://ledger/health` resource: p99 < 10 ms
  - `ledger://applications/{id}/compliance` resource: p99 < 200 ms
  - `ledger://agents/{id}/sessions/{session_id}` resource: p99 < 300 ms
  - `ledger://applications/{id}/audit-trail` resource: p99 < 500 ms
  - `ApplicationSummary` projection lag: < 500 ms under normal operation
  - `ComplianceAuditView` projection lag: < 2 000 ms under normal operation
  - Projection lag SLO tests MUST simulate 50 concurrent command handlers.

**Rationale:** Score 5 (production-ready) explicitly requires SLO-demonstrated
performance, not just functional correctness.

### III. Exact Interface Contracts (FROZEN)

The following interfaces are fixed by the challenge specification and MUST NOT
be altered in signature, return type, or semantics:

**`EventStore` class:**
```python
async def append(self, stream_id, events, expected_version, correlation_id, causation_id) -> int
async def load_stream(self, stream_id, from_position, to_position) -> list[StoredEvent]
async def load_all(self, from_global_position, event_types, batch_size) -> AsyncIterator[StoredEvent]
async def stream_version(self, stream_id) -> int
async def archive_stream(self, stream_id) -> None
async def get_stream_metadata(self, stream_id) -> StreamMetadata
```

**Aggregate pattern (both `LoanApplication` and `AgentSession`):**
```python
@classmethod
async def load(cls, store: EventStore, ...) -> "Aggregate"
def _apply(self, event: StoredEvent) -> None          # dispatches to _on_<EventType>
def _on_<EventType>(self, event: StoredEvent) -> None  # one per event type
```

**`UpcasterRegistry`:**
```python
def register(self, event_type: str, from_version: int)  # decorator
def upcast(self, event: StoredEvent) -> StoredEvent      # automatic chain application
```

**`ProjectionDaemon`:**
```python
async def run_forever(self, poll_interval_ms: int = 100) -> None
async def _process_batch(self) -> None
def get_lag(self, projection_name: str) -> int  # milliseconds
```

**MCP Tools (8 tools — command side):**
`submit_application`, `record_credit_analysis`, `record_fraud_screening`,
`record_compliance_check`, `generate_decision`, `record_human_review`,
`start_agent_session`, `run_integrity_check`

**MCP Resources (6 resources — query side):**
`ledger://applications/{id}`, `ledger://applications/{id}/compliance`,
`ledger://applications/{id}/audit-trail`, `ledger://agents/{id}/performance`,
`ledger://agents/{id}/sessions/{session_id}`, `ledger://ledger/health`

Any code change that modifies a method signature, renames a parameter, or
alters the semantics of an interface listed above MUST be flagged as a
breaking change and justified in writing.

**Rationale:** The challenge explicitly states "The interface is fixed." Other
systems in the TRP1 program will eventually write to and read from this store.

### IV. Mandatory Test Suite (NON-NEGOTIABLE)

The following tests are required deliverables — they are not optional and will
be run during assessment. The test suite MUST pass before any phase is
considered complete.

| File | Test | What it asserts |
|------|------|-----------------|
| `tests/test_concurrency.py` | Double-decision test | Two concurrent asyncio tasks append at `expected_version=3`; exactly one succeeds; one raises `OptimisticConcurrencyError`; total stream length = 4 (not 5) |
| `tests/test_upcasting.py` | Immutability test | v1 event stored; loaded as v2 via `UpcasterRegistry`; raw DB payload **unchanged** |
| `tests/test_projections.py` | SLO lag test | 50 concurrent command handlers; `ApplicationSummary` lag < 500 ms; `ComplianceAuditView` lag < 2 000 ms; `rebuild_from_scratch()` completes without downtime to live reads |
| `tests/test_gas_town.py` | Crash recovery test | 5 events appended to `AgentSession`; `reconstruct_agent_context()` called without in-memory agent; reconstructed context sufficient to continue; `NEEDS_RECONCILIATION` flag set on partial decisions |
| `tests/test_mcp_lifecycle.py` | Full lifecycle via MCP only | `start_agent_session` → `record_credit_analysis` → `record_fraud_screening` → `record_compliance_check` → `generate_decision` → `record_human_review` → `GET ledger://applications/{id}/compliance`; NO direct Python function calls permitted in this test |

Additional tests are encouraged but MUST NOT duplicate the logic of the five
mandatory tests above.

**Rationale:** The challenge assessment rubric explicitly calls out each of
these tests. A Score 3 requires the concurrency test. Score 5 requires all five.

### V. Domain Logic Encapsulation

All business rules MUST be enforced inside aggregate domain objects. No
business rule may live in a command handler, MCP tool, API layer, or
projection. A rule that is only checked in a request handler is a UI
validation, not a business rule, and is a grading failure.

**The six binding business rules (Phase 2):**
1. **Application state machine:** Valid transitions only:
   `Submitted → AwaitingAnalysis → AnalysisComplete → ComplianceReview →
   PendingDecision → ApprovedPendingHuman / DeclinedPendingHuman →
   FinalApproved / FinalDeclined`. Any out-of-order transition MUST raise
   `DomainError`.
2. **Gas Town context requirement:** An `AgentSession` aggregate MUST have an
   `AgentContextLoaded` event as its first event before any decision event
   can be appended. The aggregate enforces this; the MCP tool documents it
   as a precondition.
3. **Model version locking:** Once a `CreditAnalysisCompleted` event is
   appended for an application, no further `CreditAnalysisCompleted` events
   may be appended unless the first was superseded by `HumanReviewOverride`.
4. **Confidence floor:** A `DecisionGenerated` event with
   `confidence_score < 0.6` MUST set `recommendation = "REFER"` regardless
   of the orchestrator's analysis. This is a regulatory requirement.
5. **Compliance dependency:** An `ApplicationApproved` event MUST NOT be
   appended unless all `ComplianceRulePassed` events for the application's
   required checks are present in the `ComplianceRecord` stream.
6. **Causal chain enforcement:** Every `DecisionGenerated` event's
   `contributing_agent_sessions[]` MUST reference only `AgentSession` stream
   IDs that contain a decision event for the same `application_id`.

**Rationale:** The challenge states this explicitly. The assessment rubric
awards Score 3 for "all 6 business rules enforced" and deducts for rules
placed outside aggregates.

### VI. Architecture Documentation Standards

`DESIGN.md` and `DOMAIN_NOTES.md` are assessed with equal weight to code.
Both MUST contain quantitative analysis and explicit tradeoff reasoning.

**`DOMAIN_NOTES.md` (graded independently — due before implementation):**
Must answer all five analysis questions from Phase 0 with specificity:
1. EDA vs. ES distinction applied to a prior project component
2. One rejected aggregate boundary with coupling problem analysis
3. Exact concurrency collision trace for the dual-agent scenario
4. Projection lag consequence and UI communication strategy
5. Written upcaster for `CreditAnalysisCompleted` with inference strategy
   for `model_version` and `confidence_score` in pre-2026 historical events
6. Marten Async Daemon parallel: coordination primitive and failure mode

**`DESIGN.md` (six mandatory sections with quantitative content):**
1. Aggregate boundary justification (coupling → failure mode trace)
2. Projection strategy (Inline vs. Async; SLO commitment; snapshot strategy
   and invalidation logic for `ComplianceAuditView`)
3. Concurrency analysis (expected `OptimisticConcurrencyError` rate at 100
   concurrent applications × 4 agents; retry strategy; maximum retry budget)
4. Upcasting inference decisions (error rate quantification per inferred
   field; downstream consequence of incorrect inference; null vs. fabrication
   reasoning)
5. EventStoreDB comparison (schema → stream ID; `load_all()` → `$all`
   subscription; `ProjectionDaemon` → persistent subscriptions)
6. What you would do differently (single most significant architectural
   decision that would be reconsidered; this section MUST show reflection on
   what the implementation got wrong, not just what worked)

**Rationale:** Score 4–5 requires demonstrated architectural reasoning in
these documents. Working code with empty docs is capped at Score 3.

### VII. Allowed Technology Stack

The following is the complete allowed technology stack. No external event
store libraries may be used. No ORM abstractions over the event store schema.

| Layer | Required Choice | Forbidden |
|-------|----------------|-----------|
| Language | Python 3.12+ | Any Python < 3.12 |
| Package manager | `uv` (locked `pyproject.toml`) | pip without lock file |
| Database | PostgreSQL (latest stable) | Any other database engine |
| Async DB driver | `asyncpg` or `psycopg3` (async) | Synchronous DB drivers; SQLAlchemy ORM over event tables |
| Data validation | Pydantic v2 | dataclasses alone; marshmallow; attrs |
| Event store libs | NONE — custom implementation only | Marten, EventStoreDB client, Commanded, any pre-built event sourcing framework |
| MCP framework | `mcp` Python SDK (FastMCP or low-level) | Custom HTTP servers pretending to be MCP |
| Testing | `pytest` + `pytest-asyncio` | unittest alone |
| Hash chain | `hashlib` (stdlib) | External crypto libraries for SHA-256 |

**Reference implementations (study only, not a dependency):**
- EventStoreDB 24.x: document API mapping in `DOMAIN_NOTES.md`
- Marten 7.x + Wolverine: study Async Daemon architecture; mirror patterns

**Rationale:** The challenge mandates "PostgreSQL + asyncpg/psycopg3" as
PRIMARY. External event store libraries are explicitly excluded to ensure the
implementation demonstrates genuine understanding of the patterns.

### VIII. Phase Gate Enforcement

Each challenge phase MUST be functionally complete and its associated tests
MUST pass before work on the next phase begins. Phase skipping is prohibited.

| Phase | Gate criteria before advancing |
|-------|-------------------------------|
| Phase 0 — Domain Reconnaissance | `DOMAIN_NOTES.md` complete; all five analysis questions answered with specificity; stack orientation table documented |
| Phase 1 — Event Store Core | Schema deployed; all six `EventStore` methods implemented; `test_concurrency.py` double-decision test **passing** |
| Phase 2 — Domain Logic | All four aggregates implemented; all six business rules enforced in aggregate layer; command handler pattern applied to all seven handlers |
| Phase 3 — Projections & Async Daemon | All three projections implemented; `ProjectionDaemon` fault-tolerant; `get_lag()` exposed; SLO lag tests passing under 50-concurrent load |
| Phase 4 — Upcasting, Integrity, Gas Town | `UpcasterRegistry` implemented; `test_upcasting.py` immutability test **passing**; hash chain operational; `reconstruct_agent_context()` crash recovery test **passing** |
| Phase 5 — MCP Server | All 8 tools and 6 resources implemented; structured error types; preconditions documented in tool descriptions; `test_mcp_lifecycle.py` **passing** |
| Phase 6 (Bonus) | `run_what_if()` produces materially different outcome; `generate_regulatory_package()` independently verifiable |

**Rationale:** The challenge states "Every phase must be complete before
moving to the next." Phase 6 is required only for Score 5.

### IX. Video Demo Standard (NON-NEGOTIABLE)

The final video demo MUST satisfy the Week Standard: "Show me the complete
decision history of application ID X — from first event to final decision,
with every AI agent action, every compliance check, every human review, all
causal links intact, temporal query to any point in the lifecycle, and
cryptographic integrity verification." **This demonstration MUST complete in
under 60 seconds. A demo that exceeds 60 seconds means the week is not
complete.**

**Required demo sequence (6 minutes maximum total):**

| Segment | Duration | Content | Pass condition |
|---------|----------|---------|----------------|
| Step 1 — Week Standard | mins 1–2 | Full event history of application ID X | End-to-end < 60 s; all agent actions, compliance checks, human review, causal links, integrity verification visible |
| Step 2 — Concurrency | mins 2–3 | Double-decision test live | Two agents collide; one succeeds; one receives `OptimisticConcurrencyError` and retries; visible in output |
| Step 3 — Temporal Compliance | mins 3–4 | `ledger://applications/{id}/compliance?as_of={timestamp}` | Past state distinct from current state; demonstrably different compliance view |
| Step 4 — Upcasting & Immutability | mins 4–5 | Load v1 → arrives as v2; raw DB row unchanged | DB query shows original payload; Python load shows upcasted payload |
| Step 5 — Gas Town Recovery | mins 5–6 | Crash simulation; `reconstruct_agent_context()` | Agent resumes with correct state; no repeated work |
| Step 6 — What-If (Bonus) | mins 5–6 | Counterfactual HIGH risk tier vs. MEDIUM | Materially different final decision; cascade through business rules visible |

**Rationale:** The challenge states video requirements are non-negotiable. The
60-second Week Standard is the minimum passing bar; Steps 4–6 differentiate
Score 3 from Score 5.

## Deliverable Specification

### Interim Deliverable — Thursday 03:00 UTC

**GitHub code:**
- `src/schema.sql` — PostgreSQL schema (all four tables, all indexes, constraints)
- `src/event_store.py` — `EventStore` async class (all six methods, optimistic
  concurrency enforced, outbox writes in same transaction)
- `src/models/events.py` — Pydantic models for all event types; `BaseEvent`,
  `StoredEvent`, `StreamMetadata`; custom exceptions `OptimisticConcurrencyError`,
  `DomainError`
- `src/aggregates/loan_application.py` — `LoanApplicationAggregate` with state
  machine, `load()`, `_apply()` handlers for all loan lifecycle events
- `src/aggregates/agent_session.py` — `AgentSessionAggregate` with Gas Town
  context enforcement and model version tracking
- `src/commands/handlers.py` — `handle_credit_analysis_completed` and
  `handle_submit_application` at minimum; all follow load→validate→determine→append
- `tests/test_concurrency.py` — double-decision concurrency test **passing**
- `pyproject.toml` with locked dependencies (`uv`)
- `README.md` — install, run migrations, execute test suite

**Single PDF Report:**
- `DOMAIN_NOTES.md` content (complete, as graded deliverable)
- Architecture diagram (event store schema, aggregate boundaries, command flow)
- Progress summary (Phase 1 + Phase 2 status)
- Concurrency test results (screenshot or log output)
- Known gaps and plan for final submission

### Final Deliverable — Sunday 03:00 UTC

All six phases of source code as specified in the challenge document under
"Final — Sunday, 03:00 UTC", including all five mandatory test files passing.

**Single PDF Report:**
- `DOMAIN_NOTES.md` (finalized)
- `DESIGN.md` (finalized, all six sections with quantitative analysis)
- Architecture diagram (event store schema, aggregate boundaries, projection
  data flow, MCP tool/resource mapping)
- Concurrency & SLO analysis (test results, lag measurements, retry budgets)
- Upcasting & integrity results (immutability test output, hash chain
  verification, tamper detection demonstration)
- MCP lifecycle test results (full trace `ApplicationSubmitted → FinalApproved`
  via MCP tools only)
- Bonus results (if attempted)
- Limitations & reflection

**Video Demo:** Maximum 6 minutes; Steps 1–3 mandatory; Steps 4–6 required for
Score 5. Week Standard (Step 1) MUST complete in under 60 seconds.

## Development Workflow

### Source Layout (mandatory structure)

```
src/
├── schema.sql
├── event_store.py
├── models/
│   └── events.py
├── aggregates/
│   ├── loan_application.py
│   ├── agent_session.py
│   ├── compliance_record.py
│   └── audit_ledger.py
├── commands/
│   └── handlers.py
├── projections/
│   ├── daemon.py
│   ├── application_summary.py
│   ├── agent_performance.py
│   └── compliance_audit.py
├── upcasting/
│   ├── registry.py
│   └── upcasters.py
├── integrity/
│   ├── audit_chain.py
│   └── gas_town.py
├── mcp/
│   ├── server.py
│   ├── tools.py
│   └── resources.py
├── what_if/
│   └── projector.py          # Phase 6 bonus
└── regulatory/
    └── package.py            # Phase 6 bonus

tests/
├── test_concurrency.py       # MANDATORY
├── test_upcasting.py         # MANDATORY
├── test_projections.py       # MANDATORY
├── test_gas_town.py          # MANDATORY
└── test_mcp_lifecycle.py     # MANDATORY

DESIGN.md
DOMAIN_NOTES.md
pyproject.toml
README.md
```

### Code Quality Gates (every commit)

- No file exceeds 300 lines; refactor if approaching limit.
- No mock or stub data in any non-test file.
- No business logic outside aggregate classes.
- No blocking I/O inside async functions.
- All new public functions carry type annotations.
- All new exceptions inherit from `DomainError` or `OptimisticConcurrencyError`.

### MCP Tool Design Rules

- Every tool description MUST document its preconditions in plain English
  (the LLM description is the only contract the consuming agent has).
- All errors MUST be typed objects with `error_type`, `message`, and
  `suggested_action` fields — never bare strings.
- MCP Resources MUST NOT replay aggregate streams (except the two justified
  exceptions: `audit-trail` and `agents/{id}/sessions/{session_id}`).

## Governance

This constitution supersedes all other development practices, preferences, or
conventions for the duration of the TRP1 Week 5 engagement. No architectural
decision, technology choice, or implementation pattern may contradict the
principles above without a written justification in `DESIGN.md` that:

1. Identifies which principle is being deviated from.
2. Explains why the deviation is required (not merely convenient).
3. Documents the tradeoff accepted by taking the deviation.
4. Is reviewed and acknowledged before the deviation is implemented.

**Amendment procedure:**
- PATCH amendments (wording, typo fixes): update `LAST_AMENDED_DATE`, increment
  patch version, no additional approval required.
- MINOR amendments (new guidance, expanded sections): increment minor version,
  document in the Sync Impact Report comment above.
- MAJOR amendments (principle removal, interface contract changes): prohibited
  after Phase 1 is complete. Before Phase 1, require explicit written
  justification referencing the challenge document.

**Compliance review:**
- `DOMAIN_NOTES.md` must be reviewed against Principle VI before Phase 1 begins.
- `DESIGN.md` must be reviewed against Principle VI before Phase 5 begins.
- All five mandatory tests must be verified passing before the Final
  deliverable is submitted.
- The video demo Week Standard (under 60 seconds) must be timed and confirmed
  before submission.

**Version**: 1.0.0 | **Ratified**: 2026-03-16 | **Last Amended**: 2026-03-16
