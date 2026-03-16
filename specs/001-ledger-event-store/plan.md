# Implementation Plan: The Ledger — Agentic Event Store & Enterprise Audit Infrastructure

**Branch**: `001-ledger-event-store` | **Date**: 2026-03-16 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `specs/001-ledger-event-store/spec.md`

---

## Summary

Build a production-grade, PostgreSQL-backed event store (The Ledger) for the Apex
Financial Services multi-agent AI platform. The implementation follows the exact
challenge specification across six phases: (1) PostgreSQL schema + async EventStore
interface, (2) four domain aggregates with full business rule enforcement, (3) three
CQRS projections with async daemon and SLO-bound lag measurement, (4) automatic
upcasting registry + cryptographic audit chain + Gas Town agent memory recovery,
(5) MCP server exposing 8 typed tools and 6 resources, and (6) what-if counterfactual
projector + regulatory examination package (bonus, Score 5).

Technical approach: Python 3.12+, asyncpg for async PostgreSQL I/O, Pydantic v2
for all event/command models, MCP Python SDK for the protocol server, hashlib for
SHA-256 audit chain, pytest + pytest-asyncio for the five mandatory tests. No
external event sourcing framework. Custom implementations of every pattern.

---

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: asyncpg (or psycopg3 async), Pydantic v2, mcp (Python SDK), pytest, pytest-asyncio, hashlib (stdlib)
**Storage**: PostgreSQL (latest stable) — single database, schema.sql as specified in challenge
**Testing**: pytest + pytest-asyncio; five mandatory test files
**Target Platform**: Linux server (development on Windows via WSL or native Python)
**Project Type**: Library + MCP server + CLI (database migrations)
**Performance Goals**: EventStore append < 10 ms p99 (local); projection lag ApplicationSummary < 500 ms; ComplianceAuditView < 2,000 ms; MCP resource p99 per SLO table; Week Standard demo < 60 s end-to-end
**Constraints**: Python 3.12+ only; uv lockfile; no event store frameworks; no ORM over event tables; all interfaces exactly as written in challenge document
**Scale/Scope**: 50 concurrent command handlers for SLO tests; 1,000 applications/hour with 4 agents each for concurrency analysis in DESIGN.md

---

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate | Status |
|-----------|------|--------|
| I — Challenge Document Fidelity | All four tables, all six EventStore methods, all thirteen events, all four aggregates, all six business rules, all eight MCP tools, all six MCP resources match spec exactly | ✅ PASS — no deviations planned |
| II — Production-Grade Code Standards | All I/O async; full type annotations; daemon fault-tolerant; all 8 SLO targets tested | ✅ PASS — asyncpg async-first; Pydantic v2 throughout |
| III — Exact Interface Contracts | EventStore method signatures, aggregate load/apply, UpcasterRegistry, ProjectionDaemon, MCP tools/resources unchanged | ✅ PASS — signatures taken verbatim from challenge |
| IV — Mandatory Tests | All five test files planned and gated per phase | ✅ PASS — all five listed in project structure |
| V — Domain Logic Encapsulation | All six business rules in aggregate layer; zero business logic in handlers, MCP tools, or projections | ✅ PASS — handlers follow load→validate→determine→append; validation is in aggregate assertion methods |
| VI — Architecture Documentation | DOMAIN_NOTES.md produced before Phase 1 code; DESIGN.md with six quantitative sections + Enterprise Stack Translation before Phase 5 | ✅ PASS — both documents planned as first deliverables |
| VII — Allowed Stack | Python 3.12+, uv, asyncpg/psycopg3, PostgreSQL, Pydantic v2, no event sourcing libs | ✅ PASS — all confirmed in Technical Context above |
| VIII — Phase Gates | Phases sequenced; each gate criteria defined in constitution; no phase skipping | ✅ PASS — phase order enforced in project structure below |
| IX — Video Demo | Week Standard < 60 s; all six demo steps planned; Steps 1–3 mandatory, Steps 4–6 for Score 5 | ✅ PASS — quickstart.md includes demo runbook |

**No violations. All gates pass. Proceed to Phase 0.**

---

## Project Structure

### Documentation (this feature)

```text
specs/001-ledger-event-store/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   ├── event-store-interface.md
│   ├── aggregate-contracts.md
│   ├── mcp-tools.md
│   └── mcp-resources.md
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
src/
├── schema.sql                           # Phase 1 — exact schema from challenge
├── event_store.py                       # Phase 1 — EventStore async class
├── models/
│   └── events.py                        # Phase 1 — BaseEvent, StoredEvent, StreamMetadata,
│                                        #            all 13+ event types, exceptions
├── aggregates/
│   ├── loan_application.py              # Phase 2 — LoanApplicationAggregate, state machine
│   ├── agent_session.py                 # Phase 2 — AgentSessionAggregate, Gas Town enforcement
│   ├── compliance_record.py             # Phase 2 — ComplianceRecordAggregate
│   └── audit_ledger.py                  # Phase 2 — AuditLedgerAggregate
├── commands/
│   └── handlers.py                      # Phase 2 — all 7 command handlers
├── projections/
│   ├── daemon.py                        # Phase 3 — ProjectionDaemon, fault-tolerant
│   ├── application_summary.py           # Phase 3 — ApplicationSummary projection
│   ├── agent_performance.py             # Phase 3 — AgentPerformanceLedger projection
│   └── compliance_audit.py              # Phase 3 — ComplianceAuditView, temporal query
├── upcasting/
│   ├── registry.py                      # Phase 4A — UpcasterRegistry
│   └── upcasters.py                     # Phase 4A — CreditAnalysisCompleted v1→v2,
│                                        #            DecisionGenerated v1→v2
├── integrity/
│   ├── audit_chain.py                   # Phase 4B — run_integrity_check(), SHA-256 chain
│   └── gas_town.py                      # Phase 4C — reconstruct_agent_context()
├── mcp/
│   ├── server.py                        # Phase 5 — MCP server entry point
│   ├── tools.py                         # Phase 5 — 8 MCP tools (command side)
│   └── resources.py                     # Phase 5 — 6 MCP resources (query side)
├── what_if/
│   └── projector.py                     # Phase 6 bonus — run_what_if()
└── regulatory/
    └── package.py                       # Phase 6 bonus — generate_regulatory_package()

tests/
├── test_concurrency.py                  # MANDATORY — double-decision test
├── test_upcasting.py                    # MANDATORY — immutability test
├── test_projections.py                  # MANDATORY — SLO lag + rebuild tests
├── test_gas_town.py                     # MANDATORY — crash recovery test
└── test_mcp_lifecycle.py                # MANDATORY — full lifecycle via MCP only

DESIGN.md                                # Phase 1 — 6 sections + Enterprise Stack Translation
DOMAIN_NOTES.md                         # Phase 0 — 6 analysis questions; graded independently
pyproject.toml                           # uv locked dependencies
README.md                                # install, migrate, test, MCP server startup
```

**Structure Decision**: Single project layout as mandated by the challenge deliverables
specification and the constitution's mandatory source layout (Principle VIII).

---

## Complexity Tracking

> No constitution violations requiring justification were identified.
> This section is intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|--------------------------------------|
| `AuditLedger` direct stream load in `audit-trail` MCP resource | Justified exception documented in challenge spec ("AuditLedger stream — direct load — justified exception") | Projection would require replaying the same events; no additional read optimisation possible without denormalisation that the challenge does not ask for |
| `AgentSession` direct stream load in `agents/{id}/sessions/{session_id}` resource | Justified exception documented in challenge spec | Same rationale as above; full replay capability is the explicit goal |

Both "violations" are explicitly documented as justified exceptions in the challenge
document and are therefore compliant with Principle I (Challenge Document Fidelity).
