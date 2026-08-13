"""Integration test for the request-boundary root span + structlog span-context processor.

ADR-0129 D4 / FRE-1064. Builds a minimal FastAPI app (mirrors ``test_telemetry_router.py`` — no live
Postgres/ES/Neo4j needed) with ``RequestRootSpanMiddleware`` attached, and a
tracer provider carrying an in-memory span exporter so exported spans are
assertable. Structlog is configured with the real processor chain (including
the new ``_add_span_context`` processor) so captured log records reflect
exactly what production emits.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from personal_agent.telemetry.logger import _add_span_context
from personal_agent.telemetry.otel_middleware import RequestRootSpanMiddleware


@pytest.fixture()
def traced_app() -> Iterator[tuple[TestClient, InMemorySpanExporter]]:
    """Minimal FastAPI app with the root-span middleware and an in-memory exporter."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    app = FastAPI()
    app.add_middleware(RequestRootSpanMiddleware, tracer=tracer)

    @app.get("/probe")
    async def probe(session_id: str | None = None) -> dict[str, bool]:
        log = structlog.get_logger(__name__)
        if session_id:
            structlog.contextvars.bind_contextvars(session_id=session_id)
        try:
            log.info("probe_event_1")
            log.info("probe_event_2")
        finally:
            structlog.contextvars.clear_contextvars()
        return {"ok": True}

    with TestClient(app) as client:
        yield client, exporter


def _capture_probe_events(client: TestClient, query: str = "") -> list[MutableMapping[str, Any]]:
    """Capture events with the real span-context processor active.

    Uses ``structlog.testing.capture_logs`` — it swaps in a capturing
    processor chain for the duration of the ``with`` block and restores
    whatever was configured before, so this never leaks global structlog
    state into other test modules.
    """
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars, _add_span_context]  # type: ignore[list-item]
    ) as captured:
        response = client.get(f"/probe{query}")
        assert response.status_code == 200
    return captured


def test_served_request_opens_exactly_one_root_span(
    traced_app: tuple[TestClient, InMemorySpanExporter],
) -> None:
    """AC-1: a served request opens exactly one root span, and it is a root."""
    client, exporter = traced_app

    _capture_probe_events(client, "?session_id=11111111-1111-1111-1111-111111111111")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].parent is None


def test_log_records_carry_the_requests_span_identity(
    traced_app: tuple[TestClient, InMemorySpanExporter],
) -> None:
    """AC-2: log records carry the request's span identity.

    Every log record emitted during the request carries trace_id/span_id
    matching the root span exactly.
    """
    client, exporter = traced_app

    events = _capture_probe_events(client, "?session_id=22222222-2222-2222-2222-222222222222")

    spans = exporter.get_finished_spans()
    span = spans[0]
    expected_trace_id = format(span.context.trace_id, "032x")
    expected_span_id = format(span.context.span_id, "016x")

    assert len(events) == 2
    for event in events:
        assert event["trace_id"] == expected_trace_id
        assert event["span_id"] == expected_span_id


def test_trace_id_and_session_id_arrive_together(
    traced_app: tuple[TestClient, InMemorySpanExporter],
) -> None:
    """AC-3: trace_id and session_id arrive together or not at all.

    For a session-bearing served turn, the set of records carrying trace_id
    is exactly the set carrying session_id.
    """
    client, _exporter = traced_app

    events = _capture_probe_events(client, "?session_id=33333333-3333-3333-3333-333333333333")

    with_trace_id = {i for i, e in enumerate(events) if "trace_id" in e}
    with_session_id = {i for i, e in enumerate(events) if "session_id" in e}
    assert with_trace_id == with_session_id == {0, 1}
    for event in events:
        assert event["session_id"] == "33333333-3333-3333-3333-333333333333"


def test_no_active_span_carries_no_invented_identity() -> None:
    """AC-5: no active span means no invented identity.

    A record emitted with no active span carries no trace_id/span_id —
    absent, not a sentinel or zero id.
    """
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars, _add_span_context]  # type: ignore[list-item]
    ) as captured:
        log = structlog.get_logger(__name__)
        log.info("unspanned_event")

    assert len(captured) == 1
    assert "trace_id" not in captured[0]
    assert "span_id" not in captured[0]


def test_processor_introduces_no_second_timestamp_key(
    traced_app: tuple[TestClient, InMemorySpanExporter],
) -> None:
    """AC-6: the processor adds only trace_id/span_id — no new timestamp spelling."""
    client, _exporter = traced_app

    events = _capture_probe_events(client, "?session_id=44444444-4444-4444-4444-444444444444")

    for event in events:
        added_keys = set(event) - {"event", "session_id", "log_level"}
        assert added_keys == {"trace_id", "span_id"}


def test_root_span_middleware_wraps_cors_in_the_real_app() -> None:
    """The root span should wrap as much of a served request as possible.

    Starlette's ``add_middleware`` inserts at position 0 of ``user_middleware``,
    and ``build_middleware_stack`` wraps in ``reversed(middleware)`` order — so
    the LAST-added middleware ends up OUTERMOST, not the first. Registration
    order in ``app.py`` must put ``RequestRootSpanMiddleware`` last so it wraps
    ``CORSMiddleware``, not the reverse. Checked directly against the real app
    module (not an isolated test app) since that's the only place this ordering
    bug is observable.
    """
    from fastapi.middleware.cors import CORSMiddleware

    from personal_agent.service.app import app

    assert app.user_middleware[0].cls is RequestRootSpanMiddleware  # type: ignore[comparison-overlap]
    assert any(
        m.cls is CORSMiddleware  # type: ignore[comparison-overlap]
        for m in app.user_middleware[1:]
    )


class _StartupMarker(Exception):
    """Private marker raised to halt lifespan() startup deterministically, without
    needing live Postgres/ES/Neo4j.
    """


async def _raise_startup_marker(*_args: object, **_kwargs: object) -> None:
    raise _StartupMarker


@pytest.mark.asyncio
async def test_startup_root_span_ac1_ac2(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-1/AC-2 (ADR-0129 D3, FRE-1069) for service startup.

    ``configure_tracing`` is imported *inside* ``lifespan()`` at call time, so
    patching the source attribute (rather than a name already bound in
    ``app.py``) is picked up correctly. ``_preflight_check_tcp`` and ``init_db``
    are stubbed to succeed without live Postgres, so the real
    ``log.info("database_initialized")`` call fires — giving a genuine
    post-bootstrap record — before the marker is raised from the next step
    (the route-trace ledger connect). This proves BOTH that pre-bootstrap
    records (``service_starting``) carry no identity AND that records emitted
    after ``configure_tracing`` succeeds carry the startup span's identity even
    though ``lifespan()`` never reaches its own ``yield`` — the try/finally's
    guaranteed closure, not best-effort.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from tests._helpers.log_capture import capture_log_records

    test_provider = TracerProvider()
    exporter = InMemorySpanExporter()
    test_provider.add_span_processor(SimpleSpanProcessor(exporter))

    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "personal_agent.telemetry.otel_bootstrap.configure_tracing",
        lambda **_kwargs: test_provider,
    )
    monkeypatch.setattr("personal_agent.service.app._preflight_check_tcp", _noop)
    monkeypatch.setattr("personal_agent.service.app.init_db", _noop)
    monkeypatch.setattr(
        "personal_agent.observability.route_trace.get_route_trace_ledger",
        lambda: type("_FakeLedger", (), {"connect": _raise_startup_marker})(),
    )

    from personal_agent.service.app import app, lifespan

    with capture_log_records() as records:
        with pytest.raises(_StartupMarker):
            async with lifespan(app):
                pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.parent is None
    assert span.attributes is not None
    assert span.attributes["personal_agent.kind"] == "system:startup"

    expected_trace_id = format(span.context.trace_id, "032x")
    expected_span_id = format(span.context.span_id, "016x")

    boundary = next(i for i, r in enumerate(records) if r.get("event") == "service_starting")
    pre_bootstrap = records[: boundary + 1]
    post_bootstrap = records[boundary + 1 :]

    for record in pre_bootstrap:
        assert "trace_id" not in record
        assert "kind" not in record

    assert post_bootstrap, "expected at least one log record after configure_tracing() succeeded"
    for record in post_bootstrap:
        assert record.get("trace_id") == expected_trace_id
        assert record.get("span_id") == expected_span_id
        assert record.get("kind") == "system:startup"
