from __future__ import annotations

import argparse
import asyncio
import os
from collections import defaultdict
from pathlib import Path

import asyncpg


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Generate Week-5 API cost report from AgentSessionCompleted events.")
    parser.add_argument("--out", default="artifacts/api_cost_report.txt")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("DATABASE_URL", "postgresql://ledger:ledger_dev@localhost:5432/ledger"),
    )
    args = parser.parse_args()

    conn = await asyncpg.connect(args.dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT
              payload->>'agent_type' AS agent_type,
              payload->>'application_id' AS application_id,
              (payload->>'total_cost_usd')::double precision AS total_cost_usd
            FROM events
            WHERE event_type = 'AgentSessionCompleted'
            """
        )
    finally:
        await conn.close()

    by_agent: dict[str, float] = defaultdict(float)
    by_app: dict[str, float] = defaultdict(float)
    total = 0.0

    for r in rows:
        agent = r["agent_type"] or "unknown"
        app = r["application_id"] or "unknown"
        cost = float(r["total_cost_usd"] or 0.0)
        by_agent[agent] += cost
        by_app[app] += cost
        total += cost

    app_costs = sorted(by_app.values())
    avg = (total / len(app_costs)) if app_costs else 0.0
    min_c = app_costs[0] if app_costs else 0.0
    max_c = app_costs[-1] if app_costs else 0.0

    most_expensive = ("unknown", 0.0)
    if by_app:
        app_id = max(by_app, key=lambda k: by_app[k])
        most_expensive = (app_id, by_app[app_id])

    lines: list[str] = []
    lines.append("Apex Ledger API Cost Report")
    lines.append("=" * 40)
    lines.append(f"Total API cost (all applications): ${total:.4f}")
    lines.append(f"Average cost per application: ${avg:.4f} (range ${min_c:.4f}–${max_c:.4f})")
    lines.append("")
    lines.append("Cost by agent:")
    for agent, cost in sorted(by_agent.items()):
        lines.append(f"- {agent}: ${cost:.4f}")
    lines.append("")
    lines.append(f"Most expensive application: {most_expensive[0]} (${most_expensive[1]:.4f})")
    lines.append("")
    lines.append(f"Applications counted: {len(by_app)}")
    lines.append(f"Agent sessions counted: {len(rows)}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"OK: wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(_main())
