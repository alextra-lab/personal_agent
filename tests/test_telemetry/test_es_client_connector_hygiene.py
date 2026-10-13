"""FRE-1601: no Elasticsearch query path may discard an aiohttp connector unclosed.

The gateway logged ``Unclosed connector`` (logger ``asyncio``, level ERROR) after
turns. ``src/`` never imports ``aiohttp``, so the owner of the connector is a
transitive user: ``elasticsearch.AsyncElasticsearch`` builds one ``ClientSession``
and one ``TCPConnector`` on its first request, and nothing releases them until
``close()`` runs.

These tests run the real code against a loopback fake Elasticsearch with the event
loop in debug mode. In debug mode aiohttp records the creation stack on every
connector, so a leak names its own call site (``source_traceback``) instead of
being inferred from log timing.

The first two tests prove the detector. A detector that cannot see a known leak, or
that fires on a closed client, would make the third test meaningless.
"""

from __future__ import annotations

import asyncio
import gc
import traceback
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web

_UNCLOSED = ("Unclosed connector", "Unclosed client session")


async def _fake_es_search(request: web.Request) -> web.Response:
    """Answer any Elasticsearch request with an empty, well-formed search result."""
    request.app["request_count"] += 1
    return web.json_response(
        {
            "took": 1,
            "timed_out": False,
            "hits": {"hits": [], "total": {"value": 0, "relation": "eq"}},
            "aggregations": {"by_callsite": {"buckets": []}},
        },
        headers={"X-Elastic-Product": "Elasticsearch"},
    )


def _leaks_from(body: Callable[[], Awaitable[object]]) -> tuple[list[str], int]:
    """Run ``body`` against a fake Elasticsearch and report every unclosed aiohttp resource.

    Args:
        body: Coroutine factory that exercises the code under test. It runs while
            ``personal_agent.telemetry.queries.get_settings`` points at the fake server.

    Returns:
        A pair. The first item has one entry per ``Unclosed ...`` event that
        reached the loop's exception handler, each carrying the stack that
        created the resource. The second item is the number of requests the
        fake server received, so a test can prove that the code under test
        reached it and did not pass because it failed early.
    """

    async def main() -> tuple[list[str], int]:
        contexts: list[dict[str, object]] = []
        asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: contexts.append(ctx))

        app = web.Application()
        app["request_count"] = 0
        app.router.add_route("*", "/{tail:.*}", _fake_es_search)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        sockets = site._server.sockets  # type: ignore[union-attr]
        port = sockets[0].getsockname()[1]

        fake_settings = SimpleNamespace(
            elasticsearch_url=f"http://127.0.0.1:{port}",
            elasticsearch_index_prefix="agent-logs-test",
            captains_log_index_prefix="agent-captains-test",
        )
        try:
            with patch("personal_agent.telemetry.queries.get_settings", lambda: fake_settings):
                await body()
            gc.collect()
            await asyncio.sleep(0)
            gc.collect()
        finally:
            await runner.cleanup()

        found: list[str] = []
        for ctx in contexts:
            message = str(ctx.get("message", ""))
            if not message.startswith(_UNCLOSED):
                continue
            stack = ctx.get("source_traceback")
            created = "".join(traceback.format_list(stack[-8:])) if stack else "<no stack>"  # type: ignore[arg-type]
            found.append(f"{message}\n{created}")
        return found, int(app["request_count"])

    return asyncio.run(main(), debug=True)


def test_detector_sees_a_client_that_is_never_disconnected() -> None:
    """Seeded positive: a ``TelemetryQueries`` used and dropped is reported."""
    from personal_agent.telemetry.queries import TelemetryQueries

    async def body() -> None:
        queries = TelemetryQueries()
        await queries.get_mean_rating_by_callsite(days=7)

    leaks, requests = _leaks_from(body)
    assert requests >= 1, "the code under test never reached the fake server"
    assert leaks, "detector missed a client that was never disconnected"


def test_detector_is_quiet_for_a_disconnected_client() -> None:
    """Seeded negative: the same use, followed by ``disconnect()``, reports nothing."""
    from personal_agent.telemetry.queries import TelemetryQueries

    async def body() -> None:
        queries = TelemetryQueries()
        try:
            await queries.get_mean_rating_by_callsite(days=7)
        finally:
            await queries.disconnect()

    leaks, requests = _leaks_from(body)
    assert requests >= 1, "the code under test never reached the fake server"
    assert leaks == []


def test_load_mean_rating_lookup_releases_its_connector() -> None:
    """FRE-1601 AC-1/AC-2: the per-turn reflection lookup leaves no connector behind."""
    from personal_agent.captains_log.prompt_manifest import load_mean_rating_lookup

    async def body() -> None:
        await load_mean_rating_lookup(days=7, trace_id="fre-1601")

    leaks, requests = _leaks_from(body)
    assert requests >= 1, "the code under test never reached the fake server"
    assert leaks == []
