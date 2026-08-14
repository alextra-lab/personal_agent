"""FRE-1231 / ADR-0129 D3: the standalone gateway bootstraps its own tracer provider.

``create_gateway_app()`` (:9001, the standalone deployment) had neither an OTel SDK
bootstrap nor ``RequestRootSpanMiddleware`` — confirmed by grep before this ticket. A
served request therefore opened no span at all, and ``gateway/chat_api.py``'s
``read_or_mint_trace_id()`` call (FRE-1231) would silently fall back to minting an
unrelated id, reproducing exactly the split-identity defect FRE-1215 fixed on the main
service.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from personal_agent.telemetry.otel_bootstrap import PRE_BOOTSTRAP_LOGGERS
from tests._helpers.log_capture import capture_log_records


class _StartupMarker(Exception):
    """Private marker raised to halt gateway lifespan startup deterministically."""


async def _raise_startup_marker(*_args: object, **_kwargs: object) -> None:
    raise _StartupMarker


@pytest.mark.asyncio
async def test_gateway_lifespan_bootstraps_tracing_before_init_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The standalone gateway registers a real tracer provider before serving requests.

    ``configure_tracing`` is imported *inside* ``_gateway_lifespan()`` at call time, so
    patching the source attribute on ``otel_bootstrap`` (rather than a name already
    bound in ``gateway.app``) is picked up correctly. ``init_db`` is stubbed to raise
    the marker immediately, proving bootstrap runs *before* it — the middleware needs a
    real provider before the app can serve any request — and every log record up to and
    including ``gateway_starting_standalone`` carries no trace identity, since it was
    emitted before that bootstrap call.
    """
    test_provider = TracerProvider()
    test_provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    captured_kwargs: dict[str, object] = {}

    def _fake_configure_tracing(**kwargs: object) -> TracerProvider:
        captured_kwargs.update(kwargs)
        return test_provider

    monkeypatch.setattr(
        "personal_agent.telemetry.otel_bootstrap.configure_tracing",
        _fake_configure_tracing,
    )
    monkeypatch.setattr("personal_agent.service.database.init_db", _raise_startup_marker)

    from personal_agent.gateway.app import _gateway_lifespan

    app = FastAPI()
    with capture_log_records() as records:
        with pytest.raises(_StartupMarker):
            async with _gateway_lifespan(app):
                pass

    assert captured_kwargs["service_name"] == "personal-agent-gateway"

    boundary = next(
        i for i, r in enumerate(records) if r.get("event") == "gateway_starting_standalone"
    )
    pre_bootstrap = records[: boundary + 1]
    for record in pre_bootstrap:
        assert "trace_id" not in record


def test_gateway_app_logger_is_enumerated_pre_bootstrap() -> None:
    """``gateway_starting_standalone`` logs before ``configure_tracing()`` runs.

    ``PRE_BOOTSTRAP_LOGGERS`` claims to name every logger that can emit before
    bootstrap; ``personal_agent.gateway.app`` must be on it.
    """
    assert "personal_agent.gateway.app" in PRE_BOOTSTRAP_LOGGERS


@pytest.mark.asyncio
async def test_gateway_lifespan_shuts_down_its_own_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teardown shuts down the provider this lifespan created, not the global lookup.

    Regression guard for the provider-ownership bug found in plan review:
    ``configure_tracing()`` returns a *new* provider on every call, but
    ``trace.set_tracer_provider()`` only takes effect on the first call in a process —
    so shutting down ``opentelemetry.trace.get_tracer_provider()`` (the global) at
    teardown would, in a shared test process, shut down some *other* invocation's
    provider instead of this one's.
    """
    shutdown_calls: list[TracerProvider] = []

    class _TrackedProvider(TracerProvider):
        def shutdown(self, *args: object, **kwargs: object) -> None:
            shutdown_calls.append(self)
            super().shutdown(*args, **kwargs)

    test_provider = _TrackedProvider()
    other_global_provider = TracerProvider()  # stands in for "whichever provider is global"

    from personal_agent.config import settings

    ledger = AsyncMock()
    ledger.connect = AsyncMock(return_value=None)
    ledger.disconnect = AsyncMock(return_value=None)

    monkeypatch.setattr(otel_trace, "get_tracer_provider", lambda: other_global_provider)
    monkeypatch.setattr(
        "personal_agent.telemetry.otel_bootstrap.configure_tracing",
        lambda **_kwargs: test_provider,
    )
    monkeypatch.setattr("personal_agent.service.database.init_db", AsyncMock(return_value=None))
    monkeypatch.setattr(settings, "enable_memory_graph", False)
    monkeypatch.setattr(
        "personal_agent.observability.route_trace.get_route_trace_ledger",
        lambda: ledger,
    )

    from personal_agent.gateway.app import _gateway_lifespan

    app = FastAPI()
    with patch(
        "elasticsearch.AsyncElasticsearch", side_effect=ConnectionError("no ES in this test")
    ):
        async with _gateway_lifespan(app):
            pass

    assert shutdown_calls == [test_provider], (
        "teardown must shut down the provider this lifespan created, not the global lookup"
    )
