# The Ledger — Agentic Event Store & Enterprise Audit Infrastructure

TRP1 Week 5 | Apex Financial Services Scenario

> Build the immutable memory and governance backbone for multi-agent AI systems at production scale.

---

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Docker (for PostgreSQL)

### 1. Start PostgreSQL

```bash
docker compose up -d ledger-postgres
```

### 2. Install dependencies

```bash
uv sync
```

### 3. Run database migrations

```bash
uv run python -c "
import asyncio
from src.db.pool import create_pool, run_migrations
async def main():
    pool = await create_pool()
    await run_migrations(pool)
    await pool.close()
    print('Migrations complete.')
asyncio.run(main())
"
```

### 4. Run all tests

```bash
cd src
uv run pytest ../tests/ -v
```

Or from repo root:

```bash
uv run pytest tests/ -v
```

### 5. Start MCP Server

```bash
uv run python -m src.mcp.server
```

The server communicates on stdio (standard MCP transport).

---

## Project Structure

```
src/
  schema.sql                   # PostgreSQL schema (events, event_streams, outbox, projections)
  event_store.py               # Core EventStore async class
  __init__.py                  # Wires upcaster registry at import time
  models/
    events.py                  # Pydantic v2 event models + StoredEvent + StreamMetadata
    exceptions.py              # Typed domain exceptions (OptimisticConcurrencyError, DomainError, …)
  aggregates/
    loan_application.py        # LoanApplicationAggregate + 6 business rules
    agent_session.py           # AgentSessionAggregate + Gas Town pattern
    compliance_record.py       # ComplianceRecordAggregate
    audit_ledger.py            # AuditLedgerAggregate
  commands/
    handlers.py                # Command handlers (submit_application, record_credit_analysis, …)
  projections/
    daemon.py                  # ProjectionDaemon (async polling, fault-tolerant, SLO-aware)
    application_summary.py     # ApplicationSummary projection
    agent_performance.py       # AgentPerformanceLedger projection
    compliance_audit.py        # ComplianceAuditView projection (temporal queries + snapshots)
  upcasting/
    registry.py                # UpcasterRegistry (chain application, never mutates store)
    upcasters.py               # CreditAnalysisCompleted v1→v2, DecisionGenerated v1→v2
  integrity/
    audit_chain.py             # run_integrity_check() — SHA-256 hash chain
    gas_town.py                # reconstruct_agent_context() — crash recovery
  mcp/
    server.py                  # MCP server entry point + full wiring
    tools.py                   # 8 MCP tools (command side)
    resources.py               # 6 MCP resources (query side)
  what_if/
    projector.py               # run_what_if() — counterfactual scenario analysis
  regulatory/
    package.py                 # generate_regulatory_package() — self-contained JSON
  db/
    pool.py                    # asyncpg pool + migration runner

tests/
  conftest.py                  # Session-scoped DB pool + unique ID fixtures
  test_concurrency.py          # Double-decision OCC test (Score 3 gate)
  test_domain.py               # All 6 business rules (Score 3 gate)
  test_upcasting.py            # Immutability test (Score 4 gate)
  test_projections.py          # SLO lag tests + temporal query (Score 4 gate)
  test_gas_town.py             # Crash recovery test (Score 4 gate)
  test_mcp_lifecycle.py        # Full lifecycle via MCP tools only (Score 5 gate)
  test_what_if.py              # Counterfactual projector tests (Score 5 bonus)
  test_regulatory.py           # Regulatory package generation tests (Score 5 bonus)

scripts/
  seed_demo_application.py     # Full lifecycle seeder via MCP tool calls
  query_history.py             # Event timeline + compliance audit viewer
  what_if_demo.py              # Counterfactual scenario demo
```

---

## Key Commands

```bash
# Lint
uv run ruff check src/ tests/

# Type check (optional)
uv run mypy src/ --ignore-missing-imports

# Single test file
uv run pytest tests/test_concurrency.py -v

# Concurrency test only
uv run pytest tests/test_concurrency.py::test_double_decision_exactly_one_wins -v

# Upcasting immutability test
uv run pytest tests/test_upcasting.py -v

# Gas Town crash recovery
uv run pytest tests/test_gas_town.py -v

# Full MCP lifecycle integration test
uv run pytest tests/test_mcp_lifecycle.py -v
```

---

## Week Standard Demo (60 seconds)

```bash
# 1. Seed a complete application lifecycle (all steps via MCP tools)
uv run python scripts/seed_demo_application.py --application-id demo-001

# 2. Show complete decision history and compliance audit trail
uv run python scripts/query_history.py --application-id demo-001

# 3. Temporal compliance query (state as of a specific moment)
uv run python scripts/query_history.py --application-id demo-001 --compliance-only --as-of 2026-03-15T10:00:00Z

# 4. Run double-decision OCC test live
uv run pytest tests/test_concurrency.py -v -s

# 5. Upcasting + immutability test
uv run pytest tests/test_upcasting.py -v -s

# 6. Gas Town crash recovery
uv run pytest tests/test_gas_town.py -v -s

# 7. What-if counterfactual demo (bonus)
uv run python scripts/what_if_demo.py --application-id demo-001 --risk-tier HIGH
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://ledger:ledger_dev@localhost:5432/ledger` | PostgreSQL connection string |

Copy `.env.example` to `.env` and adjust as needed.

---

## Architecture

See [DESIGN.md](DESIGN.md) for full architectural decision records including:
- Aggregate boundary justification
- Projection SLO analysis
- Concurrency error rate estimates
- Upcasting inference decisions
- EventStoreDB and Marten comparison

See [DOMAIN_NOTES.md](DOMAIN_NOTES.md) for domain reasoning including:
- EDA vs. Event Sourcing distinction
- Concurrency trace walkthrough
- Temporal query and projection lag consequences
- Enterprise Stack Translation (PostgreSQL → Marten/Wolverine → EventStoreDB)
