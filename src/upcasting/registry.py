"""
UpcasterRegistry — centralised version migration chain for events.

Registered upcasters are applied automatically whenever old events are loaded
from the store via load_stream() or load_all(). Callers never invoke upcasters
directly.

Design (see DESIGN.md §Upcasting Inference Decisions):
  - Upcaster chain: v1→v2→v3, applied atomically in version order.
  - Stored payload is NEVER modified — upcasting is read-time only.
  - Registry is a module-level singleton; register upcasters at import time.
"""
from __future__ import annotations

from collections.abc import Callable

from src.models.events import StoredEvent


class UpcasterRegistry:
    """Registry mapping (event_type, from_version) → transform function.

    Each transform function receives the raw payload dict and returns
    a new payload dict with the updated schema. It must not mutate
    the input dict.
    """

    def __init__(self) -> None:
        self._upcasters: dict[tuple[str, int], Callable[[dict[str, object]], dict[str, object]]] = {}

    def register(
        self,
        event_type: str,
        from_version: int,
    ) -> Callable[
        [Callable[[dict[str, object]], dict[str, object]]],
        Callable[[dict[str, object]], dict[str, object]],
    ]:
        """Decorator. Registers fn as upcaster from event_type@from_version.

        Usage:
            @registry.register("CreditAnalysisCompleted", from_version=1)
            def upcast_v1_to_v2(payload: dict) -> dict:
                ...
        """

        def decorator(
            fn: Callable[[dict[str, object]], dict[str, object]],
        ) -> Callable[[dict[str, object]], dict[str, object]]:
            self._upcasters[(event_type, from_version)] = fn
            return fn

        return decorator

    def upcast(self, event: StoredEvent) -> StoredEvent:
        """Apply all registered upcasters for this event type in version order.

        Walks the chain (v, v+1, v+2, …) until no further upcaster is registered.
        Returns the event unchanged if no upcasters apply.
        """
        current = event
        v = event.event_version
        while (event.event_type, v) in self._upcasters:
            new_payload = self._upcasters[(event.event_type, v)](dict(current.payload))
            current = current.with_payload(new_payload, version=v + 1)
            v += 1
        return current

    def has_upcaster(self, event_type: str, version: int) -> bool:
        """Return True if an upcaster is registered for (event_type, version)."""
        return (event_type, version) in self._upcasters


# Module-level singleton — imported by event_store.py via set_registry()
registry = UpcasterRegistry()
