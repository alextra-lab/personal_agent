"""Tests for the request.completed ES-indexing handler (ADR-0107 AC-3b).

``build_request_trace_es_handler`` builds the consumer that indexes
request_trace/request_trace_step docs from a ``RequestCompletedEvent``. This
consumer runs in its own long-lived task (the Redis Streams consumer loop),
never the originating request's, so it cannot inherit a
``structlog.contextvars`` binding — ``user_id`` must reach the ES logger via
the event payload, not ambient context.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.events.models import RequestCompletedEvent
from personal_agent.events.request_completed_handlers import build_request_trace_es_handler


@pytest.mark.asyncio
async def test_handler_threads_user_id_into_es_logger() -> None:
    """user_id on the event must reach index_request_trace_from_snapshot."""
    user_id = uuid.uuid4()
    event = RequestCompletedEvent(
        trace_id="trace-1",
        session_id="sess-1",
        assistant_response="hi",
        trace_summary={"total_duration_ms": 1.0, "total_steps": 0, "phases_summary": {}},
        trace_breakdown=[],
        source_component="test",
        user_id=user_id,
    )

    es_handler = MagicMock()
    es_handler._connected = True
    es_handler.es_logger.index_request_trace_from_snapshot = AsyncMock(return_value="doc-1")

    handler = build_request_trace_es_handler(es_handler)
    await handler(event)

    es_handler.es_logger.index_request_trace_from_snapshot.assert_awaited_once_with(
        trace_id="trace-1",
        trace_summary=event.trace_summary,
        trace_breakdown=event.trace_breakdown,
        session_id="sess-1",
        user_id=user_id,
    )


@pytest.mark.asyncio
async def test_handler_skips_eval_mode_events() -> None:
    """Eval-mode requests must not write request_trace docs at all."""
    event = RequestCompletedEvent(
        trace_id="trace-1",
        session_id="sess-1",
        assistant_response="hi",
        trace_summary={},
        trace_breakdown=[],
        source_component="test",
        eval_mode=True,
        user_id=uuid.uuid4(),
    )
    es_handler = MagicMock()
    es_handler._connected = True
    es_handler.es_logger.index_request_trace_from_snapshot = AsyncMock()

    handler = build_request_trace_es_handler(es_handler)
    await handler(event)

    es_handler.es_logger.index_request_trace_from_snapshot.assert_not_awaited()
