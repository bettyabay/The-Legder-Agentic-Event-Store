"""
Database connection pool management.

Provides create_pool() and run_migrations() for test fixtures and application startup.
Uses asyncpg for async PostgreSQL access.
"""
from __future__ import annotations

import os
from pathlib import Path

import asyncpg


async def create_pool(dsn: str | None = None, **kwargs: object) -> asyncpg.Pool:
    """Create and return an asyncpg connection pool.

    Args:
        dsn: PostgreSQL connection string. Falls back to DATABASE_URL env var.
        **kwargs: Additional kwargs forwarded to asyncpg.create_pool().
    """
    if dsn is None:
        dsn = os.environ.get(
            "DATABASE_URL",
            "postgresql://ledger:ledger_dev@localhost:5432/ledger",
        )
    pool: asyncpg.Pool = await asyncpg.create_pool(dsn, min_size=2, max_size=20, **kwargs)
    return pool


async def run_migrations(pool: asyncpg.Pool) -> None:
    """Apply schema.sql idempotently.

    Schema uses IF NOT EXISTS guards so this is safe to call on every startup.
    """
    schema_path = Path(__file__).parent.parent / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)
