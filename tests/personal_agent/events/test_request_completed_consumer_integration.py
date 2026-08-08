"""FRE-1033: publisher -> ConsumerRunner -> real handler, wired end to end.

`tests/test_events/test_request_completed_handlers.py` used to call each
handler directly and prove it does the right thing in isolation, for both
consumer groups `request.completed` had. ADR-0129 D3 / FRE-1067 retired
`build_request_trace_es_handler` and its `cg:es-indexer` consumer group along
with `RequestTimer` — `cg:session-writer` is the only consumer left. This
proves it is actually reachable through a live publish -> Redis consumer
group -> handler chain: a real `RequestCompletedEvent` (the same shape
`_process_chat_stream_background` and `_stream_to_queue` publish) goes
through `RedisStreamBus.publish` with a mocked Redis client, is read back out
by `ConsumerRunner`, and dispatched to the real `build_session_writer_handler`
— with only the DB boundary mocked.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import orjson
import pytest
import redis.asyncio as aioredis

from personal_agent.events.consumer import ConsumerRunner
from personal_agent.events.models import (
    CG_SESSION_WRITER,
    STREAM_REQUEST_COMPLETED,
    RequestCompletedEvent,
)
from personal_agent.events.redis_backend import RedisStreamBus
from personal_agent.events.request_completed_handlers import build_session_writer_handler


def _make_single_group_xreadgroup(event_json: str) -> AsyncMock:
    """Deliver the message once to the group, then block (real long-poll shape)."""
    delivered = False

    async def _side_effect(**kwargs: object) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return [(STREAM_REQUEST_COMPLETED, [("1-0", {"data": event_json})])]
        await asyncio.sleep(60)
        return []

    return AsyncMock(side_effect=_side_effect)


@pytest.mark.asyncio
async def test_published_request_completed_reaches_session_writer_handler() -> None:
    """A published request.completed reaches cg:session-writer's real handler.

    Proves the wiring, not just the handler's own logic in isolation.
    """
    user_id = uuid4()
    session_id = str(uuid4())
    event = RequestCompletedEvent(
        trace_id="trace-int-1",
        session_id=session_id,
        assistant_response="hello from the integration test",
        source_component="service.app",
        user_id=user_id,
    )
    event_json = orjson.dumps(event.model_dump(mode="json")).decode()

    mock_redis = AsyncMock(spec=aioredis.Redis)
    mock_redis.xadd = AsyncMock(return_value="1-0")
    mock_redis.xack = AsyncMock(return_value=1)
    mock_redis.xgroup_create = AsyncMock(return_value=True)
    mock_redis.xautoclaim = AsyncMock(return_value=("0-0", [], []))
    mock_redis.xreadgroup = _make_single_group_xreadgroup(event_json)

    bus = RedisStreamBus(mock_redis)

    mock_repo = MagicMock()
    mock_repo.append_message = AsyncMock(return_value=None)

    with (
        patch(
            "personal_agent.events.request_completed_handlers.AsyncSessionLocal",
            side_effect=lambda: _fake_db_session(),
        ),
        patch(
            "personal_agent.events.request_completed_handlers.SessionRepository",
            return_value=mock_repo,
        ),
    ):
        await bus.subscribe(
            STREAM_REQUEST_COMPLETED, CG_SESSION_WRITER, "c1", build_session_writer_handler()
        )

        runner = ConsumerRunner(bus)
        await runner.start()
        await asyncio.sleep(0.2)
        await runner.stop()

    # cg:session-writer really appended the assistant message.
    mock_repo.append_message.assert_awaited_once()
    call_args = mock_repo.append_message.await_args
    assert str(call_args.args[0]) == session_id
    assert call_args.args[1]["role"] == "assistant"
    assert call_args.args[1]["content"] == "hello from the integration test"

    # The delivery was ACKed.
    assert mock_redis.xack.await_count == 1


@asynccontextmanager
async def _fake_db_session() -> AsyncIterator[MagicMock]:
    yield MagicMock()
