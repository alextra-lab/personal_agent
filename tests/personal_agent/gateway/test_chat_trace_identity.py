"""FRE-1231 / ADR-0129 D3: the standalone gateway's ``/chat`` adopts the active span's trace identity.

Precedent: ``tests/personal_agent/service/test_chat_trace_identity.py`` (FRE-1215) fixed
the same defect on ``service/app.py``'s ``/chat`` and ``/chat/stream`` — those endpoints
minted ``trace_id = str(uuid4())`` while ``RequestRootSpanMiddleware`` already had a root
span open, so Postgres recorded the minted id while Elasticsearch recorded the span's.
The standalone gateway (``gateway/chat_api.py``) had the same minting expression, but no
middleware to diverge from — until this ticket adds one (``test_gateway_otel_bootstrap.py``).

AC-1: the standalone gateway opens exactly one root span per served request, and its
response's ``trace_id`` matches that span's identity.

AC-2: the gateway's ``api_costs`` ledger row and its Elasticsearch ``model_call_completed``
event name the same trace — each derived from its own real adapter, not asserted by
construction. ``_add_span_context`` (``telemetry/logger.py``) unconditionally overwrites
``event_dict["trace_id"]`` from the *active* OTel span on every structlog record, so as
long as the span opened for a test stays current when ``_emit_gateway_model_call_completed``
runs, the ES side's identity is independently re-derived from OTel context — not merely
copied from a shared Python variable. The root-span fixture below is therefore kept open
across both the ledger call and the ES-emit call in each AC-2 test, matching the
service-side precedent's structure.

AC-4: the standalone (``create_gateway_app()``) and mounted-local (``service/app.py`` with
its default ``gateway_mount_local=True``) deployment modes serve the shared
``create_gateway_router()`` surface through the same trace-identity mechanism. Proven
against the real app objects — not a reconstruction — via two structural facts: both carry
``RequestRootSpanMiddleware`` as their outermost middleware, and both route the *literal
same* ``/api/v1/health`` handler object through it. Combined with AC-1's behavioral proof
that the middleware produces a real span-derived trace id on a real request through
``create_gateway_app()``'s own composition, and ``test_otel_root_span.py``'s pre-existing
behavioral proof of the same middleware class's mechanics, this establishes that whichever
composition serves a request, the identity mechanism is identical — without needing to
fight OpenTelemetry's per-process ``set_tracer_provider()`` singleton (only the first call
in a process takes effect) across a shared pytest session, which would make injecting a
second, independent in-memory-exporter provider into the already-imported
``service.app.app``'s middleware unreliable.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from personal_agent.gateway.chat_api import _CLOUD_MODEL, _emit_gateway_model_call_completed
from personal_agent.gateway.chat_api import router as chat_router
from personal_agent.llm_client.cost_tracker import CostTrackerService
from personal_agent.observability.joinability.walk import _normalize_trace_id
from personal_agent.service.auth import RequestUser, get_request_user
from personal_agent.telemetry.logger import _add_span_context
from personal_agent.telemetry.otel_middleware import RequestRootSpanMiddleware

_TEST_USER = RequestUser(
    user_id=UUID("00000000-0000-0000-0000-000000000001"),
    email="tester@example.com",
    display_name=None,
)
_AUTH_HEADERS = {"Cf-Access-Authenticated-User-Email": "tester@example.com"}


def _make_session_model(session_id: str) -> Any:
    session = MagicMock()
    session.session_id = session_id
    session.messages = []
    return session


@contextmanager
def _capture() -> Iterator[list[MutableMapping[str, Any]]]:
    """Capture log records through the real span-context processor."""
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars, _add_span_context]  # type: ignore[list-item]
    ) as captured:
        yield captured


# ---------------------------------------------------------------------------
# AC-1: the standalone gateway opens exactly one root span per served request
# ---------------------------------------------------------------------------


@pytest.fixture()
def traced_gateway_app() -> Iterator[tuple[TestClient, InMemorySpanExporter]]:
    """A FastAPI app composed the way ``create_gateway_app()`` composes it.

    Middleware + ``chat_router`` — the pieces AC-1 is about — with an injected
    in-memory-exporter tracer so opened spans are assertable, mirroring
    ``test_otel_root_span.py``'s ``traced_app`` fixture.
    """
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    app = FastAPI()
    app.add_middleware(RequestRootSpanMiddleware, tracer=tracer)
    app.include_router(chat_router)
    app.dependency_overrides[get_request_user] = lambda: _TEST_USER

    with TestClient(app, raise_server_exceptions=True) as client:
        yield client, exporter


def test_standalone_gateway_opens_exactly_one_root_span(
    traced_gateway_app: tuple[TestClient, InMemorySpanExporter],
) -> None:
    """AC-1: a served ``/chat`` request opens exactly one root span, and it is a root.

    Also ties AC-1 to AC-3 directly: the response body's ``trace_id`` equals that
    span's identity, not an unrelated minted id.
    """
    client, exporter = traced_gateway_app
    sid = str(uuid4())
    mock_session = _make_session_model(sid)

    with (
        patch(
            "personal_agent.gateway.chat_api.get_settings",
            return_value=MagicMock(anthropic_api_key="sk-test"),
        ),
        patch("personal_agent.gateway.chat_api.AsyncSessionLocal") as mock_session_local,
        patch(
            "personal_agent.service.repositories.session_repository.SessionRepository.get",
            new_callable=AsyncMock,
            return_value=mock_session,
        ),
        patch("asyncio.create_task"),
        _capture() as captured,
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_local.return_value = mock_ctx

        resp = client.post(
            "/chat", data={"message": "hello", "session_id": sid}, headers=_AUTH_HEADERS
        )

    assert resp.status_code == 200

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].parent is None
    expected_trace_id = format(spans[0].context.trace_id, "032x")

    assert resp.json()["trace_id"] == expected_trace_id
    for record in captured:
        if "trace_id" in record:
            assert record["trace_id"] == expected_trace_id


# ---------------------------------------------------------------------------
# AC-2: ledger row and ES telemetry independently name the same trace
# ---------------------------------------------------------------------------


class _FakeConnection:
    """asyncpg connection stand-in that records the parameters bound to each INSERT."""

    def __init__(self, calls: list[tuple[Any, ...]]) -> None:
        self._calls = calls

    async def fetchval(self, _sql: str, *params: Any) -> int:
        self._calls.append(params)
        return 11455


class _FakePool:
    """asyncpg pool stand-in exposing the ``async with pool.acquire()`` protocol."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def acquire(self) -> "_FakePool":
        return self

    async def __aenter__(self) -> _FakeConnection:
        return _FakeConnection(self.calls)

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _final_message(input_tokens: int, output_tokens: int) -> Any:
    usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    return MagicMock(usage=usage)


@pytest.mark.asyncio
async def test_gateway_ledger_row_and_es_event_name_the_same_trace() -> None:
    """AC-2: the ledger write and the ES telemetry, driven by their own real adapters.

    A real OTel span is opened directly (mirroring the service-side precedent's
    ``root_span`` fixture) and kept current across BOTH calls below — this is not
    decorative: ``_add_span_context`` overwrites ``event_dict["trace_id"]`` from the
    *active* span on every structlog record regardless of what ``trace_id`` value the
    caller passed into ``TraceContext(...)``, so the ES side's identity is independently
    re-derived from OTel context here, not merely copied from a shared string.
    """
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    tracer = provider.get_tracer("test")
    session_id = str(uuid4())

    with tracer.start_as_current_span("POST /chat") as span:
        trace_id = format(span.get_span_context().trace_id, "032x")

        # --- ledger side: the real INSERT binding -----------------------------
        tracker = CostTrackerService()
        pool = _FakePool()
        tracker.pool = pool  # type: ignore[assignment]
        await tracker.record_api_call(
            provider="anthropic",
            model=f"anthropic/{_CLOUD_MODEL}",
            input_tokens=1200,
            output_tokens=64,
            cost_usd=0.001085,
            trace_id=UUID(trace_id),
            session_id=UUID(session_id),
            purpose="main_inference",
            latency_ms=914,
        )
        ledger_rows = [{"trace_id": str(params[8])} for params in pool.calls]

        # --- ES side: the real gateway emit function, still under the same span ---
        with _capture() as captured:
            _emit_gateway_model_call_completed(
                trace_id=trace_id,
                session_id=session_id,
                span_id=format(span.get_span_context().span_id, "016x"),
                final_message=_final_message(1200, 64),
            )
    es_events = [r for r in captured if r["event"] == "model_call_completed"]

    assert len(ledger_rows) == 1, "the ledger write must have happened"
    assert len(es_events) == 1, "the ES event must have been emitted"
    assert _normalize_trace_id(ledger_rows[0]["trace_id"]) == _normalize_trace_id(
        es_events[0]["trace_id"]
    ), "the ledger row and its ES counterpart must name the same trace"
    assert _normalize_trace_id(ledger_rows[0]["trace_id"]) == trace_id, (
        "both sides must adopt the active span's identity, not an unrelated id"
    )


# ---------------------------------------------------------------------------
# AC-4: standalone and mounted-local wiring agree
# ---------------------------------------------------------------------------


def test_standalone_and_mounted_apps_share_the_same_middleware_class() -> None:
    """Both real apps carry ``RequestRootSpanMiddleware`` as their outermost middleware.

    ``service.app.app``'s side of this was already proven by
    ``test_otel_root_span.py::test_root_span_middleware_wraps_cors_in_the_real_app`` and
    is not re-asserted here; this test adds the standalone side.
    """
    from personal_agent.gateway.app import gateway_app

    assert gateway_app.user_middleware[0].cls is RequestRootSpanMiddleware  # type: ignore[comparison-overlap]


def test_standalone_and_mounted_apps_serve_the_identical_health_handler() -> None:
    """Both real apps route ``/api/v1/health`` through the literal same handler object.

    Not merely "a route exists at this path" — the identical ``create_gateway_router()``
    endpoint function is what each composition's middleware wraps, so the two deployment
    modes are not two independently-behaving copies of the health check.
    """
    from personal_agent.gateway.app import gateway_app
    from personal_agent.service.app import app as main_app

    standalone_route = next(r for r in gateway_app.routes if r.path == "/api/v1/health")  # type: ignore[attr-defined]
    mounted_route = next(r for r in main_app.routes if r.path == "/api/v1/health")  # type: ignore[attr-defined]

    assert standalone_route.endpoint is mounted_route.endpoint  # type: ignore[attr-defined]
