"""Tests for Elasticsearch handler circuit breaker behavior."""

import asyncio
import logging
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from personal_agent.telemetry.es_handler import ElasticsearchHandler


@pytest.mark.asyncio
async def test_es_handler_circuit_breaker_opens_and_recovers() -> None:
    """Open circuit after consecutive failures, then recover after cooldown."""
    handler = ElasticsearchHandler()
    handler._connected = True
    handler._circuit_breaker_threshold = 2
    handler._circuit_breaker_cooldown_s = 0.05
    cast_es_logger = cast(Any, handler.es_logger)
    cast(Any, cast_es_logger).log_event = AsyncMock(return_value=None)

    # Two failed writes should open the circuit.
    await handler._log_async("event_one", {"k": "v"}, None, None)
    assert handler._is_circuit_open() is False
    await handler._log_async("event_two", {"k": "v"}, None, None)
    assert handler._is_circuit_open() is True

    calls_before_skip = handler.es_logger.log_event.call_count
    await handler._log_async("event_three", {"k": "v"}, None, None)
    # While circuit is open, ES write is skipped.
    assert handler.es_logger.log_event.call_count == calls_before_skip

    await asyncio.sleep(0.06)
    assert handler._is_circuit_open() is False

    # Successful write after cooldown should reset failures.
    cast(Any, cast_es_logger).log_event = AsyncMock(return_value="doc-id-1")
    await handler._log_async("event_four", {"k": "v"}, None, None)
    assert handler._failure_count == 0
    assert handler._is_circuit_open() is False


@pytest.mark.asyncio
async def test_emit_forwards_session_id_to_es_logger() -> None:
    """FRE-552: session_id on a structlog record reaches es_logger.log_event payload.

    Closes the producer->ES pass-through gap that ``capture_logs`` cannot see:
    ``capture_logs`` intercepts before the stdlib bridge, so it does not cover
    the dict-pass-through in ``ElasticsearchHandler.emit``.
    """
    handler = ElasticsearchHandler()
    handler._connected = True
    cast_es_logger = cast(Any, handler.es_logger)
    cast(Any, cast_es_logger).log_event = AsyncMock(return_value="doc-id-1")

    record = logging.LogRecord(
        name="personal_agent.tools.perplexity",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg={
            "event": "perplexity_query_timeout",
            "trace_id": "trace-1",
            "session_id": "sess-552",
        },
        args=(),
        exc_info=None,
    )

    handler.emit(record)
    # emit schedules _log_async via create_task; let it run.
    await asyncio.sleep(0)

    assert handler.es_logger.log_event.await_count == 1
    args, _ = handler.es_logger.log_event.call_args
    event_type, data, trace_id = args[0], args[1], args[2]
    assert event_type == "perplexity_query_timeout"
    assert trace_id == "trace-1"
    assert data["session_id"] == "sess-552"
