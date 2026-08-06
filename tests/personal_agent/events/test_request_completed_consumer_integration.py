"""FRE-1033: publisher -> ConsumerRunner -> real handlers, wired end to end.

`tests/test_events/test_request_completed_handlers.py` calls each handler
directly and proves it does the right thing in isolation. It does not prove
the handlers are actually reachable through a live publish -> Redis consumer
group -> handler chain. This closes that gap for `request.completed`: a real
`RequestCompletedEvent` (the same shape `_process_chat_stream_background` and
`_stream_to_queue` now publish) goes through `RedisStreamBus.publish` with a
mocked Redis client, is read back out by `ConsumerRunner`, and dispatched to
the real `build_session_writer_handler` / `build_request_trace_es_handler` —
with only the DB and ES boundaries mocked.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import orjson
import pytest
import redis.asyncio as aioredis

from personal_agent.events.consumer import ConsumerRunner
from personal_agent.events.models import (
    CG_ES_INDEXER,
    CG_SESSION_WRITER,
    STREAM_REQUEST_COMPLETED,
    RequestCompletedEvent,
)
from personal_agent.events.redis_backend import RedisStreamBus
from personal_agent.events.request_completed_handlers import (
    build_request_trace_es_handler,
    build_session_writer_handler,
)


def _make_dual_group_xreadgroup(event_json: str) -> AsyncMock:
    """Deliver the same message once to each of the two request.completed consumer groups.

    Keyed by ``groupname``, since both groups' read loops call XREADGROUP
    concurrently against the same mocked client; blocks after delivery.
    """
    delivered: set[str] = set()

    async def _side_effect(**kwargs: object) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        group = kwargs["groupname"]
        if group not in delivered:
            delivered.add(group)  # type: ignore[arg-type]
            return [(STREAM_REQUEST_COMPLETED, [(f"1-0:{group}", {"data": event_json})])]
        await asyncio.sleep(60)
        return []

    return AsyncMock(side_effect=_side_effect)


@pytest.mark.asyncio
async def test_published_request_completed_reaches_both_real_handlers() -> None:
    """A published request.completed reaches both consumer groups' real handlers.

    ``cg:session-writer`` appends the assistant message and ``cg:es-indexer``
    indexes the trace — proving the wiring, not just each handler's own logic.
    """
    user_id = uuid4()
    session_id = str(uuid4())
    event = RequestCompletedEvent(
        trace_id="trace-int-1",
        session_id=session_id,
        assistant_response="hello from the integration test",
        trace_summary={"total_duration_ms": 12.3, "total_steps": 1, "phases_summary": {}},
        trace_breakdown=[
            {
                "name": "llm_call:test",
                "sequence": 1,
                "phase": "llm_inference",
                "offset_ms": 0.0,
                "duration_ms": 12.3,
            }
        ],
        source_component="service.app",
        user_id=user_id,
    )
    event_json = orjson.dumps(event.model_dump(mode="json")).decode()

    mock_redis = AsyncMock(spec=aioredis.Redis)
    mock_redis.xadd = AsyncMock(return_value="1-0")
    mock_redis.xack = AsyncMock(return_value=1)
    mock_redis.xgroup_create = AsyncMock(return_value=True)
    mock_redis.xautoclaim = AsyncMock(return_value=("0-0", [], []))
    mock_redis.xreadgroup = _make_dual_group_xreadgroup(event_json)

    bus = RedisStreamBus(mock_redis)

    es_handler = MagicMock()
    es_handler._connected = True
    es_handler.es_logger.index_request_trace_from_snapshot = AsyncMock(return_value="doc-1")

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
            STREAM_REQUEST_COMPLETED,
            CG_ES_INDEXER,
            "c0",
            build_request_trace_es_handler(es_handler),
        )
        await bus.subscribe(
            STREAM_REQUEST_COMPLETED, CG_SESSION_WRITER, "c1", build_session_writer_handler()
        )

        runner = ConsumerRunner(bus)
        await runner.start()
        await asyncio.sleep(0.2)
        await runner.stop()

    # cg:es-indexer really indexed the published trace.
    es_handler.es_logger.index_request_trace_from_snapshot.assert_awaited_once_with(
        trace_id="trace-int-1",
        trace_summary=event.trace_summary,
        trace_breakdown=event.trace_breakdown,
        session_id=session_id,
        user_id=user_id,
    )

    # cg:session-writer really appended the assistant message.
    mock_repo.append_message.assert_awaited_once()
    call_args = mock_repo.append_message.await_args
    assert str(call_args.args[0]) == session_id
    assert call_args.args[1]["role"] == "assistant"
    assert call_args.args[1]["content"] == "hello from the integration test"

    # Both deliveries were ACKed.
    assert mock_redis.xack.await_count == 2


@asynccontextmanager
async def _fake_db_session():
    yield MagicMock()
