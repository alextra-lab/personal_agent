"""The service must drain Elasticsearch delivery before closing the client (FRE-1056).

``ElasticsearchHandler.disconnect`` gained the drain in FRE-1055, but the
service lifespan is what has to *use* it, last in shutdown, so the records every
other teardown step emits are still delivered.

These tests drive ``_shutdown_es_delivery`` — the real function the lifespan
calls — rather than the shared helper underneath it. Testing the helper would
prove the helper works and say nothing about whether the service invokes it, at
the right point, once; and the helper's own drain behaviour is already covered
by ``tests/test_telemetry/test_es_handler.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from personal_agent.exceptions import ESHandlerLoopError
from personal_agent.telemetry.es_handler import ElasticsearchHandler


class _RecordingESClient:
    """Fake Elasticsearch client that records the order of index/close calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.documents: list[dict[str, Any]] = []
        self.gate = asyncio.Event()
        self.gate.set()

    async def index(self, **kwargs: Any) -> dict[str, str]:
        """Record one indexed document, after an optional release gate."""
        await self.gate.wait()
        self.calls.append("index")
        self.documents.append(kwargs)
        return {"_id": f"doc-{len(self.documents)}"}

    async def close(self) -> None:
        """Record that the client was closed."""
        self.calls.append("close")

    def record_events(self) -> list[str]:
        """Return indexed event types, excluding handler counter snapshots."""
        return [
            call["document"]["event_type"]
            for call in self.documents
            if call.get("document", {}).get("event_type") != "es_delivery_counters"
        ]


async def _connected_handler(client: _RecordingESClient) -> ElasticsearchHandler:
    """Build a connected handler backed by the recording client.

    Args:
        client: Fake Elasticsearch client to attach.

    Returns:
        A connected handler owning the current event loop.
    """
    handler = ElasticsearchHandler()
    handler.es_logger.connect = AsyncMock(return_value=True)  # type: ignore[method-assign]
    assert await handler.connect() is True
    handler.es_logger.client = client
    return handler


@pytest.mark.asyncio
async def test_service_shutdown_delivers_a_record_still_in_flight_before_closing_the_client() -> (
    None
):
    """AC2: the service drains before disconnect, at the service's own seam.

    The write is gated and the precondition asserted, so the record's arrival
    can only be explained by the drain — not by a consumer that happened to
    finish first.
    """
    client = _RecordingESClient()
    client.gate.clear()
    handler = await _connected_handler(client)
    logging.getLogger().addHandler(handler)
    try:
        from personal_agent.service.app import _shutdown_es_delivery

        handler.emit(
            logging.LogRecord(
                name="personal_agent.tests.service_shutdown",
                level=logging.WARNING,
                pathname=__file__,
                lineno=1,
                msg={"event": "record_in_flight_at_shutdown", "trace_id": "trace-svc"},
                args=(),
                exc_info=None,
            )
        )
        await asyncio.sleep(0)
        assert client.record_events() == [], "precondition: the write must still be gated"

        client.gate.set()
        await _shutdown_es_delivery(handler)
    finally:
        logging.getLogger().removeHandler(handler)

    assert "record_in_flight_at_shutdown" in client.record_events()
    # The ordering that is the whole point: the record was written, and only
    # then was the client closed.
    assert client.calls.index("index") < client.calls.index("close")


@pytest.mark.asyncio
async def test_service_shutdown_deregisters_captains_log_producers() -> None:
    """Producers stop being handed the handler the shutdown is about to tear down."""
    from personal_agent.captains_log import capture as capture_module
    from personal_agent.captains_log.es_indexer import get_es_indexer
    from personal_agent.captains_log.manager import CaptainLogManager
    from personal_agent.service.app import _shutdown_es_delivery

    client = _RecordingESClient()
    handler = await _connected_handler(client)
    capture_module.set_default_es_handler(handler)
    CaptainLogManager.set_default_es_handler(handler)

    await _shutdown_es_delivery(handler)

    assert capture_module._default_es_handler is None
    assert CaptainLogManager._default_es_handler is None
    assert get_es_indexer() is None


@pytest.mark.asyncio
async def test_handler_is_detached_even_when_disconnect_raises() -> None:
    """A failing disconnect must not strand the handler on the root logger.

    Without the ``finally`` in ``detach_elasticsearch_handler`` the handler stays
    attached with a dead client behind it, charging every subsequent log record
    against a pipeline that can no longer deliver.
    """
    from personal_agent.telemetry import detach_elasticsearch_handler

    client = _RecordingESClient()
    handler = await _connected_handler(client)
    logging.getLogger().addHandler(handler)
    handler.disconnect = AsyncMock(  # type: ignore[method-assign]
        side_effect=ESHandlerLoopError("owner loop is gone")
    )

    with pytest.raises(ESHandlerLoopError):
        await detach_elasticsearch_handler(handler)

    assert handler not in logging.getLogger().handlers
