"""
MCP Server entry point for The Ledger.

Wires together:
  - EventStore (asyncpg pool)
  - UpcasterRegistry (auto-applied on load_stream / load_all)
  - ProjectionDaemon (background task)
  - MCP tools (command side)
  - MCP resources (query side)

Start:
    uv run python -m src.mcp.server
"""
from __future__ import annotations

import asyncio
import logging
import os

import asyncpg
from mcp.server import Server
from mcp.server.stdio import stdio_server

from src.db.pool import create_pool, run_migrations
from src.event_store import EventStore, set_registry
from src.mcp.resources import register_resources
from src.mcp.tools import register_tools
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon
from src.upcasting.registry import registry

# Register upcasters at import time (side-effect import)
import src.upcasting.upcasters  # noqa: F401

logger = logging.getLogger(__name__)


async def create_server(pool: asyncpg.Pool | None = None) -> tuple[Server, asyncpg.Pool, ProjectionDaemon]:
    """Create and configure a fully-wired MCP server.

    Returns (server, pool, daemon) so callers can manage lifecycle.
    """
    if pool is None:
        pool = await create_pool()
    await run_migrations(pool)

    # Wire upcaster registry into EventStore
    set_registry(registry)

    store = EventStore(pool=pool)

    # Build projections
    summary_proj = ApplicationSummaryProjection()
    agent_perf_proj = AgentPerformanceLedgerProjection()
    compliance_proj = ComplianceAuditViewProjection(pool=pool)

    daemon = ProjectionDaemon(
        store=store,
        projections=[summary_proj, agent_perf_proj, compliance_proj],
        pool=pool,
    )

    server = Server("the-ledger")
    register_tools(server, store)
    register_resources(server, store, pool, daemon, compliance_proj)

    return server, pool, daemon


async def main() -> None:
    """Entry point: start MCP server and projection daemon."""
    logging.basicConfig(level=logging.INFO)

    pool = await create_pool(os.environ.get("DATABASE_URL"))
    server, pool, daemon = await create_server(pool=pool)

    # Start daemon as background task
    asyncio.create_task(daemon.run_forever(poll_interval_ms=100))

    logger.info("The Ledger MCP server starting on stdio transport")
    async with stdio_server() as streams:
        await server.run(*streams)


if __name__ == "__main__":
    asyncio.run(main())
