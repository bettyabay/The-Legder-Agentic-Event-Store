"""
Seed the event store from starter/data/seed_events.jsonl (Phase 1).

Reads the JSONL file, groups events by stream_id in order, and inserts
them into events, event_streams, and outbox. This uses direct SQL so
any event_type in the file is accepted (no Pydantic event classes needed).

Phase 1 check:
  1. uv run python scripts/run_migrations.py
  2. uv run python scripts/verify_schema.py
  3. uv run python scripts/seed_from_jsonl.py
  4. uv run python -c "
     import asyncio
     from src.db.pool import create_pool
     from src.event_store import EventStore
     async def main():
         pool = await create_pool()
         store = EventStore(pool)
         events = await store.load_stream('loan-APEX-0001')
         print('loan-APEX-0001 event count:', len(events))
         await pool.close()
     asyncio.run(main())
     "

Usage:
  uv run python scripts/seed_from_jsonl.py
  uv run python scripts/seed_from_jsonl.py --file starter/data/seed_events.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from src.db.pool import create_pool


def _default_dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://ledger:ledger_dev@localhost:5432/ledger",
    )


def _parse_recorded_at(val: object) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        # JSONL timestamps may be naive ISO (no timezone). Assume UTC.
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    raise TypeError(f"Unsupported recorded_at type: {type(val).__name__}")


async def seed_from_jsonl(file_path: Path, dsn: str) -> None:
    """Read JSONL and insert into events + event_streams + outbox."""
    pool = await create_pool(dsn)
    stream_version: dict[str, int] = {}

    try:
        async with pool.acquire() as conn:
            with open(file_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    stream_id = rec["stream_id"]
                    event_type = rec["event_type"]
                    event_version = int(rec.get("event_version", 1))
                    payload = rec["payload"]
                    recorded_at = _parse_recorded_at(rec.get("recorded_at"))

                    current = stream_version.get(stream_id, 0)
                    new_pos = current + 1

                    async with conn.transaction():
                        if new_pos == 1:
                            aggregate_type = stream_id.split("-")[0]
                            await conn.execute(
                                """
                                INSERT INTO event_streams
                                  (stream_id, aggregate_type, current_version)
                                VALUES ($1, $2, 0)
                                ON CONFLICT (stream_id) DO NOTHING
                                """,
                                stream_id,
                                aggregate_type,
                            )

                        row = await conn.fetchrow(
                            """
                            INSERT INTO events
                              (stream_id, stream_position, event_type, event_version,
                               payload, metadata, recorded_at)
                            VALUES ($1, $2, $3, $4, $5::jsonb, '{}'::jsonb,
                                    COALESCE($6::timestamptz, clock_timestamp()))
                            RETURNING event_id
                            """,
                            stream_id,
                            new_pos,
                            event_type,
                            event_version,
                            json.dumps(payload, default=str),
                            recorded_at,
                        )
                        event_id = row["event_id"]

                        await conn.execute(
                            """
                            UPDATE event_streams
                            SET current_version = $1
                            WHERE stream_id = $2
                            """,
                            new_pos,
                            stream_id,
                        )

                        outbox_payload = {
                            "event_type": event_type,
                            "stream_id": stream_id,
                            **payload,
                        }
                        await conn.execute(
                            """
                            INSERT INTO outbox (event_id, destination, payload)
                            VALUES ($1, $2, $3::jsonb)
                            """,
                            event_id,
                            "internal",
                            json.dumps(outbox_payload, default=str),
                        )

                    stream_version[stream_id] = new_pos

        print(f"Seeded {sum(stream_version.values())} events across {len(stream_version)} streams.")
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed event store from JSONL")
    parser.add_argument(
        "--file",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "starter" / "data" / "seed_events.jsonl",
        help="Path to JSONL file",
    )
    parser.add_argument(
        "--dsn",
        default=_default_dsn(),
        help="PostgreSQL DSN (default: DATABASE_URL or ledger dev)",
    )
    args = parser.parse_args()
    if not args.file.exists():
        raise SystemExit(f"File not found: {args.file}")
    asyncio.run(seed_from_jsonl(args.file, args.dsn))


if __name__ == "__main__":
    main()
