"""
The Ledger — Agentic Event Store & Enterprise Audit Infrastructure.

Import this package to get a fully-configured EventStore with upcasters wired.
"""
from __future__ import annotations

# Wire upcaster registry into EventStore at package import time
from src.event_store import set_registry
from src.upcasting.registry import registry

# Side-effect import: registers all upcasters
import src.upcasting.upcasters  # noqa: F401

set_registry(registry)
