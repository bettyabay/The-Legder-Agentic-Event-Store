"""
Shared test fixtures for The Ledger test suite.

All tests use real PostgreSQL — no database mocking.
Each test generates unique stream IDs to prevent cross-test pollution.
"""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from src.db.pool import create_pool, run_migrations


@pytest_asyncio.fixture
async def db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Per-test asyncpg connection pool (same event loop as the test).

    Session scope would bind the pool to a different event loop than the one
    running each test, causing 'Future attached to a different loop' with asyncpg.
    """
    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql://ledger:ledger_dev@localhost:5432/ledger",
    )
    pool = await create_pool(dsn)
    await run_migrations(pool)
    yield pool
    await pool.close()


@pytest.fixture
def unique_app_id() -> str:
    """Return a unique application_id for test isolation."""
    return f"test-{uuid4()}"


@pytest.fixture
def unique_agent_id() -> str:
    """Return a unique agent_id for test isolation."""
    return f"agent-{uuid4()}"


@pytest.fixture
def unique_session_id() -> str:
    """Return a unique session_id for test isolation."""
    return f"session-{uuid4()}"
