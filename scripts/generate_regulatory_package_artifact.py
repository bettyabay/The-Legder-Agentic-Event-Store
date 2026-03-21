from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from src.db.pool import create_pool
from src.event_store import EventStore
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.regulatory.package import generate_regulatory_package


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Generate regulatory package JSON artifact.")
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--out", default="artifacts/regulatory_package.json")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("DATABASE_URL", "postgresql://ledger:ledger_dev@localhost:5432/ledger"),
    )
    args = parser.parse_args()

    pool = await create_pool(dsn=args.dsn)
    try:
        store = EventStore(pool)
        compliance_proj = ComplianceAuditViewProjection(pool)
        package = await generate_regulatory_package(
            store=store,
            compliance_proj=compliance_proj,
            application_id=args.application_id,
            examination_date=datetime.now(tz=timezone.utc),
        )
    finally:
        await pool.close()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(package.to_json(), encoding="utf-8")
    print(f"OK: wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(_main())
