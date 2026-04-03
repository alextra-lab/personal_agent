"""EventBus protocol and no-op implementation (ADR-0041).

Follows the singleton pattern established by
``brainstem.sensors.metrics_daemon.{set,get}_global_metrics_daemon``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from personal_agent.events.models import EventBase
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

# Type alias for subscription handlers.
EventHandler = Callable[[EventBase], Awaitable[None]]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EventBus(Protocol):
    """Abstract event bus interface.

    Concrete implementations:
    - ``NoOpBus``  — silent discard (feature flag off / fallback).
    - ``RedisStreamBus`` — Redis Streams backend.
    """

    async def publish(self, stream: str, event: EventBase) -> None:
        """Publish an event to a stream.

        Args:
            stream: Target stream name (e.g. ``stream:request.captured``).
            event: Event payload.
        """
        ...

    async def subscribe(
        self,
        stream: str,
        group: str,
        consumer_name: str,
        handler: EventHandler,
    ) -> None:
        """Register a consumer-group subscription.

        The actual reading loop is driven by ``ConsumerRunner``; this method
        records the subscription metadata so the runner knows what to read.

        Args:
            stream: Stream name to consume from.
            group: Consumer group name.
            consumer_name: Unique consumer name within the group.
            handler: Async callback invoked for each event.
        """
        ...

    async def close(self) -> None:
        """Release underlying connections."""
        ...


# ---------------------------------------------------------------------------
# No-op implementation (feature flag off or Redis unavailable)
# ---------------------------------------------------------------------------


class NoOpBus:
    """Event bus that silently discards all publishes.

    Used when ``event_bus_enabled`` is ``False`` or when Redis is
    unreachable at startup (graceful degradation).
    """

    async def publish(self, stream: str, event: EventBase) -> None:
        """Discard the event."""

    async def subscribe(
        self,
        stream: str,
        group: str,
        consumer_name: str,
        handler: EventHandler,
    ) -> None:
        """No-op subscribe."""

    async def close(self) -> None:
        """Nothing to close."""


# ---------------------------------------------------------------------------
# Process-global singleton
# ---------------------------------------------------------------------------

_global_event_bus: EventBus = NoOpBus()


def set_global_event_bus(bus: EventBus) -> None:
    """Set the process-global event bus instance.

    Args:
        bus: EventBus implementation to use globally.
    """
    global _global_event_bus
    _global_event_bus = bus
    log.info("event_bus_set", bus_type=type(bus).__name__)


def get_event_bus() -> EventBus:
    """Get the process-global event bus instance.

    Returns:
        The currently configured EventBus (defaults to ``NoOpBus``).
    """
    return _global_event_bus
