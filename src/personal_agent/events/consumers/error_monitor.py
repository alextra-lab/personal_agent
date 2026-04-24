"""Bus consumer that drives ErrorMonitor on each consolidation.completed event.

Subscribes ``cg:error-monitor`` to ``stream:consolidation.completed``.
One scan fires per event; all heavy logic lives in ``telemetry.error_monitor``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from personal_agent.events.models import ConsolidationCompletedEvent, EventBase
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.telemetry.error_monitor import ErrorMonitor

log = get_logger(__name__)


class ErrorMonitorConsumer:
    """Stateless consumer that delegates to :class:`~personal_agent.telemetry.error_monitor.ErrorMonitor`.

    Args:
        monitor: Configured ``ErrorMonitor`` instance.
        enabled: When ``False``, all events are silently ignored (feature flag).
    """

    def __init__(self, monitor: ErrorMonitor, enabled: bool = True) -> None:
        """Initialise with a configured monitor and optional feature flag."""
        self._monitor = monitor
        self._enabled = enabled

    async def handle(self, event: EventBase) -> None:
        """Accept an event from the consumer runner.

        Args:
            event: Incoming event. Only ``ConsolidationCompletedEvent`` triggers
                a scan; all other types are silently ignored.
        """
        if not self._enabled:
            return
        if not isinstance(event, ConsolidationCompletedEvent):
            return
        log.debug(
            "error_monitor_consumer_triggered",
            event_id=event.event_id,
            captures_processed=event.captures_processed,
        )
        await self._monitor.scan()
