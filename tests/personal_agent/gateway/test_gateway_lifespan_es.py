"""The standalone gateway must ship its own logs to Elasticsearch (FRE-1056).

FRE-1051 established that ``add_elasticsearch_handler`` had exactly one call
site, in the FastAPI service lifespan. The standalone gateway built a handler
purely to harvest its Elasticsearch client for read queries and never attached
it, so the gateway process shipped no logs at all.

These tests drive the **real** ``_gateway_lifespan`` with a **real**
``ElasticsearchHandler``. Only ``AsyncElasticsearch`` itself is faked, plus the
storage backends the Elasticsearch path does not touch. Everything the criteria
are actually about — root attachment, the delivery queue, the drain, the detach
— runs for real, because a test that mocks the seam owning the behaviour passes
whether or not the behaviour is there.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider

from personal_agent.telemetry.es_handler import ElasticsearchHandler

_TEST_LOGGER = "personal_agent.tests.gateway_lifespan"


class _FakeESClient:
    """Minimal AsyncElasticsearch stand-in that records what it was asked to do."""

    def __init__(self, *, handshake_fails: bool = False) -> None:
        self.documents: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.block_writes = asyncio.Event()
        self.block_writes.set()
        self._handshake_fails = handshake_fails

    async def info(self) -> dict[str, Any]:
        """Report a version, as the real client's handshake does.

        Raises:
            ConnectionError: When built with ``handshake_fails``, reproducing an
                Elasticsearch that accepts the client construction and then
                fails the handshake.
        """
        if self._handshake_fails:
            raise ConnectionError("elasticsearch handshake refused")
        return {"version": {"number": "8.13.0"}}

    async def index(self, **kwargs: Any) -> dict[str, str]:
        """Record one indexed document, optionally after a release gate."""
        await self.block_writes.wait()
        self.calls.append("index")
        self.documents.append(kwargs)
        return {"_id": f"doc-{len(self.documents)}"}

    async def close(self) -> None:
        """Record that the client was closed."""
        self.calls.append("close")

    def record_documents(self) -> list[dict[str, Any]]:
        """Return indexed log records, excluding handler counter snapshots.

        Returns:
            Documents whose ``event_type`` is not the delivery-counter export.
        """
        return [
            call["document"]
            for call in self.documents
            if call.get("document", {}).get("event_type") != "es_delivery_counters"
        ]


@pytest.fixture
def isolated_root_logger() -> Iterator[None]:
    """Restore the root logger's handlers and level after the test.

    These tests are the first in the suite to run a lifespan that attaches a
    handler to the root logger. Without isolation a failure part-way through
    would leak that handler into every later test.

    Yields:
        Control to the test.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def _attached_es_handlers() -> list[ElasticsearchHandler]:
    """Return every ElasticsearchHandler currently on the root logger."""
    return [h for h in logging.getLogger().handlers if isinstance(h, ElasticsearchHandler)]


def _es_handlers_added_since(before: list[ElasticsearchHandler]) -> list[ElasticsearchHandler]:
    """Return ES handlers attached since a baseline snapshot.

    A raw count of attached handlers is a global assertion, and the suite has
    already proved it can be perturbed: a manual test attached a handler and
    only disconnected it, leaving it on the root logger for every later test.
    Measuring the delta asserts what this criterion is actually about — the
    lifespan attaching *its* handler, exactly once — without depending on what
    else ran first.

    Args:
        before: Handlers observed before the lifespan started.

    Returns:
        Handlers present now that were not present then.
    """
    seen = {id(h) for h in before}
    return [h for h in _attached_es_handlers() if id(h) not in seen]


def _emit(event: str) -> None:
    """Emit one structlog-shaped record through the root logger."""
    logging.getLogger(_TEST_LOGGER).warning({"event": event, "trace_id": "trace-gw"})


async def _run_lifespan(app: FastAPI, client: _FakeESClient) -> AsyncIterator[None]:
    """Enter the real gateway lifespan with only non-ES backends faked.

    Args:
        app: Application whose ``state`` the lifespan populates.
        client: Fake Elasticsearch client the handler will connect through.

    Yields:
        Control while the lifespan context is open.
    """
    from personal_agent.config import settings
    from personal_agent.gateway.app import _gateway_lifespan

    ledger = AsyncMock()
    ledger.connect = AsyncMock(return_value=None)
    ledger.disconnect = AsyncMock(return_value=None)

    with (
        patch.object(settings, "enable_memory_graph", False),
        patch("personal_agent.service.database.init_db", new=AsyncMock(return_value=None)),
        patch(
            "personal_agent.observability.route_trace.get_route_trace_ledger",
            return_value=ledger,
        ),
        patch("elasticsearch.AsyncElasticsearch", return_value=client),
        # FRE-1231: this file's subject is the Elasticsearch handler, not tracing —
        # patch configure_tracing (source attribute, since _gateway_lifespan imports it
        # locally at call time) to a lightweight no-exporter provider so these four
        # tests don't each spin up a real OTel bootstrap / BatchSpanProcessor thread.
        patch(
            "personal_agent.telemetry.otel_bootstrap.configure_tracing",
            return_value=TracerProvider(),
        ),
    ):
        async with _gateway_lifespan(app):
            yield


@pytest.mark.asyncio
async def test_gateway_lifespan_attaches_handler_to_root_logger(
    isolated_root_logger: None,
) -> None:
    """AC1: the standalone gateway attaches its handler, so its logs ship at all."""
    app = FastAPI()
    client = _FakeESClient()
    before = _attached_es_handlers()

    async for _ in _run_lifespan(app, client):
        added = _es_handlers_added_since(before)
        assert len(added) == 1, "gateway lifespan did not attach exactly one ES handler"
        assert added[0] is app.state.es_handler
        assert app.state.es_client is not None


@pytest.mark.asyncio
async def test_gateway_lifespan_drains_a_record_still_in_flight_at_shutdown(
    isolated_root_logger: None,
) -> None:
    """AC1: shutdown drains, proven on a record that had provably not landed yet.

    Emitting and finding the document afterwards would prove nothing — the
    consumer may have delivered it long before shutdown began. The write is
    blocked and the precondition asserted, so only the drain can explain the
    document's arrival.
    """
    app = FastAPI()
    client = _FakeESClient()
    client.block_writes.clear()

    async for _ in _run_lifespan(app, client):
        _emit("gateway_record_in_flight")
        await asyncio.sleep(0)
        assert client.record_documents() == [], "precondition: the write must still be blocked"
        client.block_writes.set()

    delivered = [d["event_type"] for d in client.record_documents()]
    assert "gateway_record_in_flight" in delivered


@pytest.mark.asyncio
async def test_gateway_lifespan_closes_the_client_when_the_handshake_fails(
    isolated_root_logger: None,
) -> None:
    """A handler that never became usable is still torn down, not dropped.

    ``ElasticsearchLogger.connect`` assigns ``self.client`` *before* the
    handshake and does not clear it when the handshake raises, so dropping the
    reference — which this lifespan used to do — leaks an open client. Nothing
    else in the suite covers this branch, and it is the one the leak fix lives
    on, so without this test the property could regress silently.
    """
    app = FastAPI()
    client = _FakeESClient(handshake_fails=True)
    before = _attached_es_handlers()

    async for _ in _run_lifespan(app, client):
        # An unusable Elasticsearch must not attach a handler at all.
        assert _es_handlers_added_since(before) == []
        assert app.state.es_handler is None
        assert app.state.es_client is None

    assert "close" in client.calls, "the dangling client from the failed handshake was not closed"


@pytest.mark.asyncio
async def test_gateway_lifespan_detaches_handler_on_shutdown(
    isolated_root_logger: None,
) -> None:
    """Shutdown leaves no handler behind, and no stale client on app.state."""
    app = FastAPI()
    client = _FakeESClient()
    before = _attached_es_handlers()

    async for _ in _run_lifespan(app, client):
        assert len(_es_handlers_added_since(before)) == 1

    assert _es_handlers_added_since(before) == []
    assert app.state.es_handler is None
    assert app.state.es_client is None
    assert "close" in client.calls
