"""Tests for Elasticsearch handler circuit breaker behavior."""

import asyncio
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
