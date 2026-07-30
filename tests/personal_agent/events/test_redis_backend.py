"""Tests for RedisStreamBus with mocked Redis (ADR-0041)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import redis.asyncio as aioredis

from personal_agent.events.models import RequestCapturedEvent
from personal_agent.events.redis_backend import RedisStreamBus

from .conftest import _capturing_log


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Create a mocked redis.asyncio.Redis client."""
    client = AsyncMock(spec=aioredis.Redis)
    client.ping = AsyncMock(return_value=True)
    client.xadd = AsyncMock(return_value="1234567890-0")
    client.xgroup_create = AsyncMock(return_value=True)
    client.xack = AsyncMock(return_value=1)
    client.aclose = AsyncMock()
    return client


@pytest.fixture
def bus(mock_redis: AsyncMock) -> RedisStreamBus:
    """Create a RedisStreamBus with a mocked client."""
    return RedisStreamBus(mock_redis)


class TestPublish:
    """RedisStreamBus.publish() tests."""

    @pytest.mark.asyncio
    async def test_publish_calls_xadd(self, bus: RedisStreamBus, mock_redis: AsyncMock) -> None:
        """publish() calls XADD with serialized event data."""
        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        await bus.publish("stream:request.captured", event)
        mock_redis.xadd.assert_called_once()
        call_args = mock_redis.xadd.call_args
        assert call_args[0][0] == "stream:request.captured"
        payload = call_args[0][1]
        assert "data" in payload

    @pytest.mark.asyncio
    async def test_publish_serializes_event_as_json(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """Published payload contains valid JSON with event fields."""
        import orjson

        event = RequestCapturedEvent(trace_id="abc", session_id="def", source_component="test")
        await bus.publish("stream:test", event)
        payload = mock_redis.xadd.call_args[0][1]
        parsed = orjson.loads(payload["data"])
        assert parsed["trace_id"] == "abc"
        assert parsed["session_id"] == "def"
        assert parsed["event_type"] == "request.captured"

    @pytest.mark.asyncio
    async def test_publish_log_does_not_collide_with_es_event_type(
        self, bus: RedisStreamBus
    ) -> None:
        """FRE-1066: the log call must not pass event_type= (see redis_backend.py::publish)."""
        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        mock_log, calls = _capturing_log()
        with patch("personal_agent.events.redis_backend.log", mock_log):
            await bus.publish("stream:request.captured", event)
        published = [kw for name, kw in calls if name == "event_published"]
        assert published, "expected an event_published log call"
        assert "event_type" not in published[0]
        assert published[0]["payload_event_type"] == "request.captured"
        assert published[0]["stream"] == "stream:request.captured"


class TestSubscribe:
    """RedisStreamBus.subscribe() tests."""

    @pytest.mark.asyncio
    async def test_subscribe_creates_group(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """subscribe() calls XGROUP CREATE with MKSTREAM."""

        async def handler(e: object) -> None:
            pass

        await bus.subscribe("stream:test", "cg:test", "c0", handler)
        mock_redis.xgroup_create.assert_called_once_with(
            "stream:test", "cg:test", id="0", mkstream=True
        )

    @pytest.mark.asyncio
    async def test_subscribe_handles_busygroup(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """subscribe() tolerates BUSYGROUP (group already exists)."""
        mock_redis.xgroup_create.side_effect = aioredis.ResponseError(
            "BUSYGROUP Consumer Group name already exists"
        )

        async def handler(e: object) -> None:
            pass

        # Should not raise
        await bus.subscribe("stream:test", "cg:test", "c0", handler)

    @pytest.mark.asyncio
    async def test_subscribe_registers_subscription(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """subscribe() adds to subscriptions list."""

        async def handler(e: object) -> None:
            pass

        await bus.subscribe("stream:test", "cg:test", "c0", handler)
        assert len(bus.subscriptions) == 1
        sub = bus.subscriptions[0]
        assert sub.stream == "stream:test"
        assert sub.group == "cg:test"
        assert sub.consumer_name == "c0"

    @pytest.mark.asyncio
    async def test_subscribe_reraises_non_busygroup_error(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """subscribe() raises non-BUSYGROUP ResponseError."""
        mock_redis.xgroup_create.side_effect = aioredis.ResponseError("SOME OTHER ERROR")

        async def handler(e: object) -> None:
            pass

        with pytest.raises(aioredis.ResponseError, match="SOME OTHER ERROR"):
            await bus.subscribe("stream:test", "cg:test", "c0", handler)


class TestAck:
    """RedisStreamBus.ack() tests."""

    @pytest.mark.asyncio
    async def test_ack_calls_xack(self, bus: RedisStreamBus, mock_redis: AsyncMock) -> None:
        await bus.ack("stream:test", "cg:test", "1234-0")
        mock_redis.xack.assert_called_once_with("stream:test", "cg:test", "1234-0")


class TestDeadLetter:
    """RedisStreamBus.dead_letter() tests."""

    @pytest.mark.asyncio
    async def test_dead_letter_publishes_to_dl_stream(
        self, bus: RedisStreamBus, mock_redis: AsyncMock
    ) -> None:
        """dead_letter() writes to the configured dead-letter stream."""
        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        await bus.dead_letter(
            event=event,
            source_stream="stream:test",
            group="cg:test",
            error="boom",
            attempts=3,
        )
        mock_redis.xadd.assert_called_once()
        call_args = mock_redis.xadd.call_args
        assert call_args[0][0] == "stream:dead_letter"
        payload = call_args[0][1]
        assert payload["source_stream"] == "stream:test"
        assert payload["consumer_group"] == "cg:test"
        assert payload["error"] == "boom"
        assert payload["attempts"] == "3"

    @pytest.mark.asyncio
    async def test_dead_letter_log_does_not_collide_with_es_event_type(
        self, bus: RedisStreamBus
    ) -> None:
        """FRE-1066: same collision as publish() (see redis_backend.py::dead_letter)."""
        event = RequestCapturedEvent(trace_id="t1", session_id="s1", source_component="test")
        mock_log, calls = _capturing_log()
        with patch("personal_agent.events.redis_backend.log", mock_log):
            await bus.dead_letter(
                event=event,
                source_stream="stream:test",
                group="cg:test",
                error="boom",
                attempts=3,
            )
        dead_lettered = [kw for name, kw in calls if name == "event_dead_lettered"]
        assert dead_lettered, "expected an event_dead_lettered log call"
        assert "event_type" not in dead_lettered[0]
        assert dead_lettered[0]["payload_event_type"] == "request.captured"


class TestClose:
    """RedisStreamBus.close() tests."""

    @pytest.mark.asyncio
    async def test_close_calls_aclose(self, bus: RedisStreamBus, mock_redis: AsyncMock) -> None:
        await bus.close()
        mock_redis.aclose.assert_called_once()


class TestConnect:
    """RedisStreamBus.connect() factory tests."""

    @pytest.mark.asyncio
    async def test_connect_pings_redis(self) -> None:
        """connect() verifies connectivity with PING."""
        mock_client = AsyncMock(spec=aioredis.Redis)
        mock_client.ping = AsyncMock(return_value=True)

        with patch(
            "personal_agent.events.redis_backend.aioredis.from_url", return_value=mock_client
        ):
            bus = await RedisStreamBus.connect("redis://localhost:6379/0")
            mock_client.ping.assert_called_once()
            assert bus.client is mock_client
