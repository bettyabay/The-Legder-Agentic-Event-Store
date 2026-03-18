"""
Run database migrations (apply src/schema.sql).

Usage:
  uv run python scripts/run_migrations.py
"""
from __future__ import annotations

import asyncio

from src.db.pool import create_pool, run_migrations


async def main() -> None:
    pool = await create_pool()
    try:
        await run_migrations(pool)
        print("Migrations complete.")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
