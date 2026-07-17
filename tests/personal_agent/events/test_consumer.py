"""Tests for ConsumerRunner with mocked Redis (ADR-0041)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import redis.asyncio as aioredis

from personal_agent.events.consumer import ConsumerRunner
from personal_agent.events.models import RequestCapturedEvent
from personal_agent.events.redis_backend import RedisStreamBus


async def _default_xreadgroup(
    **kwargs: object,
) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
    """Default XREADGROUP mock: yields control instead of returning synchronously.

    A synchronous ``return_value=[]`` never suspends the awaiting task, so a
    ``_read_loop`` iteration with no messages spins forever without ever handing
    control back to the event loop (real Redis blocks for ``block_ms``, which is
    a genuine suspension). ``asyncio.sleep(0)`` is the minimal real yield point
    that avoids starving sibling coroutines (e.g. a test's own ``asyncio.sleep``)
    without slowing the suite down with real wall-clock waits.
    """
    await asyncio.sleep(0)
    return []


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Create a mocked redis.asyncio.Redis client."""
    client = AsyncMock(spec=aioredis.Redis)
    client.xreadgroup = AsyncMock(side_effect=_default_xreadgroup)
    client.xadd = AsyncMock(return_value="1-0")
    client.xack = AsyncMock(return_value=1)
    client.xgroup_create = AsyncMock(return_value=True)
    client.xautoclaim = AsyncMock(return_value=("0-0", [], []))
    client.aclose = AsyncMock()
    return client


@pytest.fixture
def bus(mock_redis: AsyncMock) -> RedisStreamBus:
    """Create a RedisStreamBus with a mocked client."""
    return RedisStreamBus(mock_redis)


def _make_xreadgroup_side_effect(
    messages: list[tuple[str, list[tuple[str, dict[str, str]]]]],
) -> AsyncMock:
    """Build an xreadgroup side_effect that yields messages then blocks forever.

    After delivering all messages, the mock raises ``CancelledError`` so
    the consumer loop exits without spinning.
    """
    call_count = 0

    async def _side_effect(**kwargs: object) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        nonlocal call_count
        call_count += 1
        if call_count <= len(messages):
            return [messages[call_count - 1]]
        # Block until cancelled (simulates XREADGROUP BLOCK with no new data)
        await asyncio.sleep(60)
        return []

    return AsyncMock(side_effect=_side_effect)


class TestConsumerRunner:
    """ConsumerRunner lifecycle tests."""

    @pytest.mark.asyncio
    async def test_start_creates_tasks_for_subscriptions(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """start() creates one asyncio.Task per subscription."""

        async def handler(e: object) -> None:
            pass

        await bus.subscribe("stream:test", "cg:test", "c0", handler)
        runner = ConsumerRunner(bus)
        await runner.start()
        assert len(runner._tasks) == 1
        await runner.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_tasks(self, bus: RedisStreamBus, mock_redis: AsyncMock) -> None:
        """stop() cancels all running tasks."""

        async def handler(e: object) -> None:
            pass

        await bus.subscribe("stream:test", "cg:test", "c0", handler)
        runner = ConsumerRunner(bus)
        await runner.start()
        await runner.stop()
        assert len(runner._tasks) == 0

    @pytest.mark.asyncio
    async def test_processes_message_and_acks(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """Runner dispatches event to handler and ACKs on success."""
        import orjson

        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        event_json = orjson.dumps(event.model_dump(mode="json")).decode()

        received_events: list[object] = []

        async def handler(e: object) -> None:
            received_events.append(e)

        mock_redis.xreadgroup = _make_xreadgroup_side_effect(
            [("stream:test", [("1-0", {"data": event_json})])]
        )

        await bus.subscribe("stream:test", "cg:test", "c0", handler)
        runner = ConsumerRunner(bus)
        await runner.start()
        # Give the loop time to process the message
        await asyncio.sleep(0.1)
        await runner.stop()

        assert len(received_events) == 1
        ev = received_events[0]
        assert isinstance(ev, RequestCapturedEvent)
        assert ev.trace_id == "t1"
        assert ev.session_id == "s1"
        mock_redis.xack.assert_called_once_with("stream:test", "cg:test", "1-0")

    @pytest.mark.asyncio
    async def test_handler_error_triggers_dead_letter(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """After max_retries handler failures, event is dead-lettered with attempts and ACKed."""
        import orjson

        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        event_json = orjson.dumps(event.model_dump(mode="json")).decode()

        handler_calls = 0

        async def failing_handler(e: object) -> None:
            nonlocal handler_calls
            handler_calls += 1
            raise ValueError("processing failed")

        mock_redis.xreadgroup = _make_xreadgroup_side_effect(
            [("stream:test", [("1-0", {"data": event_json})])]
        )

        await bus.subscribe("stream:test", "cg:test", "c0", failing_handler)
        runner = ConsumerRunner(bus)
        await runner.start()
        # Three attempts with backoff 0.05 + 0.1 s between failures
        await asyncio.sleep(0.5)
        await runner.stop()

        assert handler_calls == 3
        assert mock_redis.xadd.call_count >= 1
        assert mock_redis.xack.call_count >= 1
        dl_args = mock_redis.xadd.call_args_list[-1][0]
        assert dl_args[1]["attempts"] == "3"

    @pytest.mark.asyncio
    async def test_handler_succeeds_after_transient_failures(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """Handler failures below max_retries do not dead-letter; success ACKs."""
        import orjson

        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        event_json = orjson.dumps(event.model_dump(mode="json")).decode()

        calls = 0

        async def flaky_handler(e: object) -> None:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError("transient")

        mock_redis.xreadgroup = _make_xreadgroup_side_effect(
            [("stream:test", [("1-0", {"data": event_json})])]
        )

        await bus.subscribe("stream:test", "cg:test", "c0", flaky_handler)
        runner = ConsumerRunner(bus)
        await runner.start()
        await asyncio.sleep(0.5)
        await runner.stop()

        assert calls == 3
        mock_redis.xack.assert_called_once_with("stream:test", "cg:test", "1-0")
        assert mock_redis.xadd.call_count == 0

    @pytest.mark.asyncio
    async def test_missing_data_field_acks_and_skips(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """Messages without a 'data' field are ACKed and skipped."""
        mock_redis.xreadgroup = _make_xreadgroup_side_effect(
            [("stream:test", [("1-0", {"wrong_field": "nope"})])]
        )

        handled: list[object] = []

        async def handler(e: object) -> None:
            handled.append(e)

        await bus.subscribe("stream:test", "cg:test", "c0", handler)
        runner = ConsumerRunner(bus)
        await runner.start()
        await asyncio.sleep(0.1)
        await runner.stop()

        assert len(handled) == 0
        mock_redis.xack.assert_called_once_with("stream:test", "cg:test", "1-0")


class TestClaimStuckMessages:
    """XAUTOCLAIM self-reclaim sweep (FRE-906, ADR-0041 ack-timeout wiring)."""

    @pytest.mark.asyncio
    async def test_claims_and_reprocesses_stuck_message_on_startup(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """A PEL entry idle past the ack timeout is reclaimed and dispatched."""
        import orjson

        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        event_json = orjson.dumps(event.model_dump(mode="json")).decode()

        mock_redis.xautoclaim = AsyncMock(return_value=("0-0", [("2-0", {"data": event_json})], []))

        received_events: list[object] = []

        async def handler(e: object) -> None:
            received_events.append(e)

        await bus.subscribe("stream:test", "cg:test", "c0", handler)
        runner = ConsumerRunner(bus)
        await runner.start()
        await asyncio.sleep(0.1)
        await runner.stop()

        assert len(received_events) == 1
        assert isinstance(received_events[0], RequestCapturedEvent)
        mock_redis.xack.assert_called_once_with("stream:test", "cg:test", "2-0")
        mock_redis.xautoclaim.assert_any_call(
            name="stream:test",
            groupname="cg:test",
            consumername="c0",
            min_idle_time=300_000,
            start_id="0-0",
            count=10,
        )

    @pytest.mark.asyncio
    async def test_claim_sweep_paginates_across_cursor(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """A non-terminal cursor is fed back into the next XAUTOCLAIM call."""
        import orjson

        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        event_json = orjson.dumps(event.model_dump(mode="json")).decode()

        call_count = 0

        async def _side_effect(**kwargs: object) -> tuple[str, list[object], list[object]]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ("5-0", [("2-0", {"data": event_json})], [])
            return ("0-0", [("3-0", {"data": event_json})], [])

        mock_redis.xautoclaim = AsyncMock(side_effect=_side_effect)

        received_events: list[object] = []

        async def handler(e: object) -> None:
            received_events.append(e)

        await bus.subscribe("stream:test", "cg:test", "c0", handler)
        runner = ConsumerRunner(bus)
        await runner.start()
        await asyncio.sleep(0.1)
        await runner.stop()

        assert len(received_events) == 2
        assert mock_redis.xautoclaim.call_count == 2
        second_call_kwargs = mock_redis.xautoclaim.call_args_list[1].kwargs
        assert second_call_kwargs["start_id"] == "5-0"

    @pytest.mark.asyncio
    async def test_claim_sweep_runs_periodically(
        self, bus: RedisStreamBus, mock_redis: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sweep re-fires roughly every ack-timeout interval, not just once."""
        from types import SimpleNamespace

        import personal_agent.events.consumer as consumer_module

        fake_settings = SimpleNamespace(
            event_bus_consumer_poll_interval_ms=10,
            event_bus_max_retries=3,
            event_bus_ack_timeout_seconds=0.05,
        )
        monkeypatch.setattr(consumer_module, "get_settings", lambda: fake_settings)

        await bus.subscribe("stream:test", "cg:test", "c0", AsyncMock())
        runner = ConsumerRunner(bus)
        await runner.start()
        await asyncio.sleep(0.3)
        await runner.stop()

        assert mock_redis.xautoclaim.call_count >= 2

    @pytest.mark.asyncio
    async def test_claim_sweep_error_does_not_crash_read_loop(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """XAUTOCLAIM failure is logged and swallowed; normal message flow continues."""
        import orjson

        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        event_json = orjson.dumps(event.model_dump(mode="json")).decode()

        mock_redis.xautoclaim = AsyncMock(side_effect=ConnectionError("redis unreachable"))
        mock_redis.xreadgroup = _make_xreadgroup_side_effect(
            [("stream:test", [("1-0", {"data": event_json})])]
        )

        received_events: list[object] = []

        async def handler(e: object) -> None:
            received_events.append(e)

        await bus.subscribe("stream:test", "cg:test", "c0", handler)
        runner = ConsumerRunner(bus)
        await runner.start()
        await asyncio.sleep(0.1)
        await runner.stop()

        assert len(received_events) == 1
        mock_redis.xack.assert_called_once_with("stream:test", "cg:test", "1-0")

    @pytest.mark.asyncio
    async def test_reclaimed_message_exhausting_retries_is_dead_lettered_once(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """A reclaimed message whose handler always fails is dead-lettered exactly once."""
        import orjson

        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        event_json = orjson.dumps(event.model_dump(mode="json")).decode()

        mock_redis.xautoclaim = AsyncMock(return_value=("0-0", [("2-0", {"data": event_json})], []))

        handler_calls = 0

        async def failing_handler(e: object) -> None:
            nonlocal handler_calls
            handler_calls += 1
            raise ValueError("still broken")

        await bus.subscribe("stream:test", "cg:test", "c0", failing_handler)
        runner = ConsumerRunner(bus)
        await runner.start()
        await asyncio.sleep(0.5)
        await runner.stop()

        assert handler_calls == 3
        assert mock_redis.xadd.call_count == 1
        mock_redis.xack.assert_called_once_with("stream:test", "cg:test", "2-0")

    @pytest.mark.asyncio
    async def test_reclaimed_malformed_message_is_acked_and_skipped(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """A reclaimed message missing the 'data' field is ACKed, handler never runs."""
        mock_redis.xautoclaim = AsyncMock(
            return_value=("0-0", [("2-0", {"wrong_field": "nope"})], [])
        )

        handled: list[object] = []

        async def handler(e: object) -> None:
            handled.append(e)

        await bus.subscribe("stream:test", "cg:test", "c0", handler)
        runner = ConsumerRunner(bus)
        await runner.start()
        await asyncio.sleep(0.1)
        await runner.stop()

        assert len(handled) == 0
        mock_redis.xack.assert_called_once_with("stream:test", "cg:test", "2-0")
