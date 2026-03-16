# Quickstart: The Ledger — Agentic Event Store

**Branch**: `001-ledger-event-store` | **Date**: 2026-03-16

---

## Prerequisites

- Python 3.12+ (`python --version`)
- PostgreSQL 15+ running locally or via Docker
- `uv` package manager (`pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)

---

## 1. Install Dependencies

```bash
# From project root
uv sync                          # installs all locked deps from pyproject.toml

# Verify
uv run python --version          # should print Python 3.12.x
```

Expected `pyproject.toml` structure:
```toml
[project]
name = "the-ledger"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "asyncpg>=0.29",
    "pydantic>=2.0",
    "mcp>=1.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]
```

---

## 2. Database Setup

### 2a. Start PostgreSQL (Docker)

```bash
docker run -d \
  --name ledger-postgres \
  -e POSTGRES_DB=ledger \
  -e POSTGRES_USER=ledger \
  -e POSTGRES_PASSWORD=ledger_dev \
  -p 5432:5432 \
  postgres:16-alpine
```

### 2b. Configure Environment

```bash
# .env (never commit; copy from .env.example)
DATABASE_URL=postgresql://ledger:ledger_dev@localhost:5432/ledger
```

### 2c. Run Migrations

```bash
uv run python -m src.db.migrate
# OR directly:
psql $DATABASE_URL -f src/schema.sql
```

Expected output:
```
CREATE TABLE events
CREATE TABLE event_streams
CREATE TABLE projection_checkpoints
CREATE TABLE outbox
CREATE TABLE compliance_snapshots
CREATE TABLE projection_errors
All indexes created.
Migration complete.
```

---

## 3. Run the Test Suite

### All mandatory tests (must all pass):

```bash
uv run pytest tests/ -v
```

### Run tests individually (for phase-by-phase development):

```bash
# Phase 1 — concurrency test
uv run pytest tests/test_concurrency.py -v

# Phase 4 — upcasting immutability
uv run pytest tests/test_upcasting.py -v

# Phase 3 — projection SLOs
uv run pytest tests/test_projections.py -v

# Phase 4C — Gas Town crash recovery
uv run pytest tests/test_gas_town.py -v

# Phase 5 — full MCP lifecycle
uv run pytest tests/test_mcp_lifecycle.py -v
```

### Test database configuration:

Tests use the same `DATABASE_URL` env var. Each test creates isolated streams
using `application_id = f"test-{uuid4()}"` to prevent cross-test pollution.
No test mocks the database — all tests run against real PostgreSQL.

---

## 4. Start the MCP Server

```bash
# stdio transport (default for MCP client integration)
uv run python -m src.mcp.server

# SSE transport (for browser/HTTP clients)
uv run python -m src.mcp.server --transport sse --port 8000
```

The server starts the `ProjectionDaemon` as a background asyncio task automatically.

---

## 5. Run the Week Standard Demo

> "Show me the complete decision history of application ID X"
> Must complete in under 60 seconds (Constitution Principle IX).

### Step 1 — Seed a complete loan lifecycle (requires the MCP server running):

```bash
uv run python scripts/seed_demo_application.py --application-id demo-001
```

This script calls MCP tools in sequence:
1. `submit_application` (ApplicationSubmitted)
2. `start_agent_session` × 3 (CreditAnalysis, FraudDetection, DecisionOrchestrator agents)
3. `record_credit_analysis` (CreditAnalysisCompleted)
4. `record_fraud_screening` (FraudScreeningCompleted)
5. `record_compliance_check` × N (ComplianceRulePassed × required rules)
6. `generate_decision` (DecisionGenerated)
7. `record_human_review` (HumanReviewCompleted → ApplicationApproved)

### Step 2 — Query the complete history:

```bash
# Full audit trail (ledger resource)
uv run python scripts/query_history.py --application-id demo-001

# Compliance state at a past timestamp
uv run python scripts/query_history.py \
  --application-id demo-001 \
  --as-of "2026-03-15T12:00:00Z"

# Verify cryptographic integrity
uv run python -m src.integrity.audit_chain demo-001
```

### Expected output (< 60 seconds end-to-end):
```
=== Decision History: demo-001 ===
Event 1 [global:1]  ApplicationSubmitted        2026-03-16T10:00:00Z
Event 2 [global:4]  CreditAnalysisRequested     2026-03-16T10:00:01Z
Event 3 [global:8]  CreditAnalysisCompleted     2026-03-16T10:00:05Z  [v2, upcasted: no]
Event 4 [global:12] FraudScreeningCompleted     2026-03-16T10:00:06Z
Event 5 [global:20] ComplianceRulePassed × 3    2026-03-16T10:00:08Z
Event 6 [global:25] DecisionGenerated           2026-03-16T10:00:10Z  [APPROVE, conf=0.87]
Event 7 [global:30] HumanReviewCompleted        2026-03-16T10:00:15Z  [override=false]
Event 8 [global:31] ApplicationApproved         2026-03-16T10:00:15Z

Integrity check: chain_valid=True, tamper_detected=False (8 events verified)
Demo time: 4.2 seconds
```

---

## 6. Video Demo Runbook (6 Minutes)

### Minutes 1–3: Required Steps

**Step 1 — Week Standard** (< 60 s):
```bash
time uv run python scripts/query_history.py --application-id demo-001
# Assert: completes in under 60 seconds
```

**Step 2 — Concurrency Under Pressure**:
```bash
uv run pytest tests/test_concurrency.py -v -s
# Shows: two concurrent asyncio tasks, one OptimisticConcurrencyError, one success
```

**Step 3 — Temporal Compliance Query**:
```bash
# In a different terminal, first check current state:
uv run python scripts/query_history.py --application-id demo-001 --compliance-only

# Then query past state (before approval):
uv run python scripts/query_history.py \
  --application-id demo-001 \
  --compliance-only \
  --as-of "2026-03-15T09:00:00Z"
# Shows: different compliance state — demonstrates temporal query
```

### Minutes 4–6: Mastery Steps

**Step 4 — Upcasting & Immutability**:
```bash
uv run pytest tests/test_upcasting.py -v -s
# Shows: v1 event stored, loaded as v2, raw DB row unchanged
```

**Step 5 — Gas Town Recovery**:
```bash
uv run pytest tests/test_gas_town.py -v -s
# Shows: 5 events appended, reconstruct_agent_context() called, context sufficient
```

**Step 6 — What-If Counterfactual** (Phase 6 bonus):
```bash
uv run python scripts/what_if_demo.py \
  --application-id demo-001 \
  --risk-tier HIGH
# Shows: DECLINE outcome vs original APPROVE — business rules cascade correctly
```

---

## 7. Phase Gate Checklist

Before moving to each phase, verify all tests pass:

| Phase | Gate Command | Must Pass |
|-------|-------------|-----------|
| Phase 1 → 2 | `uv run pytest tests/test_concurrency.py` | Double-decision test |
| Phase 2 → 3 | Domain integration tests (TBD in tasks.md) | All 6 business rules tested |
| Phase 3 → 4 | `uv run pytest tests/test_projections.py` | Lag SLO + rebuild tests |
| Phase 4 → 5 | `uv run pytest tests/test_upcasting.py tests/test_gas_town.py` | Immutability + recovery |
| Phase 5 → 6 | `uv run pytest tests/test_mcp_lifecycle.py` | Full lifecycle via MCP only |

---

## 8. Common Errors & Fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `asyncpg.exceptions.UniqueViolationError` | Race on `UNIQUE (stream_id, stream_position)` | This should be caught and re-raised as `OptimisticConcurrencyError` in `EventStore.append` |
| `StreamNotFoundError` | `load_stream` called before `submit_application` | Call `submit_application` first |
| `DomainError(rule='context_not_loaded')` | Agent tool called without `start_agent_session` | Call `start_agent_session` first |
| `DomainError(rule='confidence_floor_violated')` | `confidence_score < 0.6` with APPROVE/DECLINE recommendation | Override recommendation to REFER in command |
| `ProjectionDaemon` lag > SLO | Slow handler in projection | Check `projection_errors` table; fix handler |
