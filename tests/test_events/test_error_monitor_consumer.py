"""Tests for ErrorMonitorConsumer (ADR-0056 §step 5).

Tests verify:
1. ConsolidationCompletedEvent triggers ErrorMonitor.scan()
2. Other event types are silently ignored
3. Consumer is disabled when error_monitor_enabled=False
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.events.consumers.error_monitor import ErrorMonitorConsumer
from personal_agent.events.models import ConsolidationCompletedEvent, RequestCompletedEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_consolidation_event() -> ConsolidationCompletedEvent:
    return ConsolidationCompletedEvent(
        source_component="brainstem.scheduler",
        captures_processed=3,
        entities_created=2,
        entities_promoted=1,
    )


def _make_consumer(enabled: bool = True) -> tuple[ErrorMonitorConsumer, AsyncMock]:
    mock_monitor = AsyncMock()
    mock_monitor.scan.return_value = []
    consumer = ErrorMonitorConsumer(monitor=mock_monitor, enabled=enabled)
    return consumer, mock_monitor


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consolidation_event_triggers_scan() -> None:
    """ConsolidationCompletedEvent causes ErrorMonitor.scan() to be called."""
    consumer, mock_monitor = _make_consumer()

    await consumer.handle(_make_consolidation_event())

    mock_monitor.scan.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_event_types_are_ignored() -> None:
    """Non-ConsolidationCompletedEvent events do not trigger a scan."""
    consumer, mock_monitor = _make_consumer()
    other_event = RequestCompletedEvent(
        source_component="orchestrator",
        trace_id="tid-1",
        session_id="sid-1",
        assistant_response="hello",
        trace_summary={},
        trace_breakdown=[],
    )

    await consumer.handle(other_event)

    mock_monitor.scan.assert_not_awaited()


@pytest.mark.asyncio
async def test_consumer_disabled_skips_scan() -> None:
    """When enabled=False, handle() is a no-op regardless of event type."""
    consumer, mock_monitor = _make_consumer(enabled=False)

    await consumer.handle(_make_consolidation_event())

    mock_monitor.scan.assert_not_awaited()
