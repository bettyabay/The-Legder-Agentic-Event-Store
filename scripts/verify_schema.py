"""
Verify that the Phase 1 database schema exists and list tables.

Use this after run_migrations to confirm events, event_streams,
projection_checkpoints, and outbox are present.

Usage:
  uv run python scripts/verify_schema.py
"""
from __future__ import annotations

import asyncio
import os

from src.db.pool import create_pool


async def main() -> None:
    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql://ledger:ledger_dev@localhost:5432/ledger",
    )
    pool = await create_pool(dsn)
    try:
        async with pool.acquire() as conn:
            # List tables in public schema (Phase 1 core tables)
            rows = await conn.fetch(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name
                """
            )
            tables = [r["table_name"] for r in rows]
            print("Tables in public schema:")
            for t in tables:
                print(f"  - {t}")

            phase1_core = {"events", "event_streams", "projection_checkpoints", "outbox"}
            missing = phase1_core - set(tables)
            if missing:
                print(f"\nMissing Phase 1 core tables: {missing}")
                print("Run: uv run python scripts/run_migrations.py")
            else:
                print("\nPhase 1 core tables present.")

            # Optional: row counts
            if "events" in tables:
                n = await conn.fetchval("SELECT COUNT(*) FROM events")
                print(f"  events row count: {n}")
            if "event_streams" in tables:
                n = await conn.fetchval("SELECT COUNT(*) FROM event_streams")
                print(f"  event_streams row count: {n}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
