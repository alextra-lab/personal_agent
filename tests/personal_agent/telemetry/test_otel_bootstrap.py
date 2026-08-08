"""Unit tests for the OpenTelemetry tracer-provider bootstrap (ADR-0129 D4 / FRE-1064).

AC-7: this ticket wires no export path — the configured provider must carry
no OTLP or network span exporter.
"""

from __future__ import annotations

from opentelemetry.propagate import get_global_textmap
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from personal_agent.telemetry.otel_bootstrap import configure_tracing


def test_configure_tracing_returns_a_tracer_provider() -> None:
    """configure_tracing() returns a real SDK TracerProvider instance."""
    provider = configure_tracing(service_name="test-service")

    assert isinstance(provider, TracerProvider)


def test_configure_tracing_installs_no_span_processor() -> None:
    """AC-7: no exporter is wired by this ticket, so no span processor exists yet."""
    provider = configure_tracing(service_name="test-service")

    processors = provider._active_span_processor._span_processors  # noqa: SLF001
    assert processors == ()


def test_configure_tracing_resource_carries_service_name() -> None:
    """The provider's Resource carries the given service.name attribute."""
    provider = configure_tracing(service_name="test-service")

    assert provider.resource.attributes.get("service.name") == "test-service"


def test_configure_tracing_sets_explicit_w3c_propagator() -> None:
    """AC-14 depends on ``opentelemetry.propagate.inject`` using W3C traceparent —
    set explicitly at bootstrap rather than relying on the SDK's default
    (ADR-0129 D1: explicit propagation over implicit convention).
    """
    configure_tracing(service_name="test-service")

    assert isinstance(get_global_textmap(), TraceContextTextMapPropagator)
