"""FRE-1715: FastAPI's native OpenTelemetry stays off on the gateway app.

FastAPI 0.142 starts spans, metrics and logs for every request once a global
provider exists, and the gateway configures one. ``RequestRootSpanMiddleware``
(ADR-0129 D4) owns the request root span, so a framework span above it changes
every trace, and an inbound ``traceparent`` becomes the parent of a turn.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from personal_agent.service.app import app


def test_a_request_records_no_framework_span_or_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    """With global providers set, a request adds nothing under the FastAPI scope."""
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: tracer_provider)
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: meter_provider)

    TestClient(app).get("/fre-1715-no-such-route")

    scopes = {
        span.instrumentation_scope.name
        for span in exporter.get_finished_spans()
        if span.instrumentation_scope is not None
    }
    assert "fastapi" not in scopes
    data = reader.get_metrics_data()
    metric_scopes = (
        {sm.scope.name for rm in data.resource_metrics for sm in rm.scope_metrics}
        if data is not None
        else set()
    )
    assert "fastapi" not in metric_scopes
