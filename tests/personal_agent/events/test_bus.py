"""Tests for EventBus protocol, NoOpBus, and singleton (ADR-0041)."""

from unittest.mock import patch

import pytest

from personal_agent.events.bus import (
    EventBus,
    NoOpBus,
    get_event_bus,
    set_global_event_bus,
)
from personal_agent.events.models import RequestCapturedEvent

from .conftest import _capturing_log


class TestNoOpBus:
    """NoOpBus silently discards everything."""

    @pytest.mark.asyncio
    async def test_publish_is_silent(self) -> None:
        bus = NoOpBus()
        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        # Should not raise
        await bus.publish("stream:test", event)

    @pytest.mark.asyncio
    async def test_publish_log_does_not_collide_with_es_event_type(self) -> None:
        """FRE-1066: event_type= must not be passed directly (see bus.py::NoOpBus.publish)."""
        bus = NoOpBus()
        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        mock_log, calls = _capturing_log()
        with patch("personal_agent.events.bus.log", mock_log):
            await bus.publish("stream:request.captured", event)
        discarded = [kw for name, kw in calls if name == "event_discarded_noop_bus"]
        assert discarded, "expected an event_discarded_noop_bus log call"
        assert "event_type" not in discarded[0]
        assert discarded[0]["payload_event_type"] == "request.captured"
        assert discarded[0]["stream"] == "stream:request.captured"

    @pytest.mark.asyncio
    async def test_subscribe_is_silent(self) -> None:
        bus = NoOpBus()

        async def handler(e: object) -> None:
            pass

        await bus.subscribe("stream:test", "cg:test", "c0", handler)

    @pytest.mark.asyncio
    async def test_close_is_silent(self) -> None:
        bus = NoOpBus()
        await bus.close()


class TestProtocol:
    """EventBus protocol compliance."""

    def test_noop_bus_satisfies_protocol(self) -> None:
        assert isinstance(NoOpBus(), EventBus)


class TestSingleton:
    """Global event bus singleton."""

    def test_default_is_noop(self) -> None:
        """Default global bus is NoOpBus."""
        # Reset to default
        set_global_event_bus(NoOpBus())
        bus = get_event_bus()
        assert isinstance(bus, NoOpBus)

    def test_set_and_get(self) -> None:
        """set_global_event_bus / get_event_bus round-trip."""
        custom = NoOpBus()
        set_global_event_bus(custom)
        assert get_event_bus() is custom
        # Restore default
        set_global_event_bus(NoOpBus())
