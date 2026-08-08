"""Unit tests for the OpenTelemetry tracer-provider bootstrap (ADR-0129 D4/D5, FRE-1064/FRE-1070).

FRE-1064 AC-7: with no endpoint given, the configured provider carries no OTLP or network
span exporter. FRE-1070 attaches the Collector exporter when an endpoint is given.
"""

from __future__ import annotations

from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import get_global_textmap
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from personal_agent.telemetry.otel_bootstrap import configure_tracing


def test_configure_tracing_returns_a_tracer_provider() -> None:
    """configure_tracing() returns a real SDK TracerProvider instance."""
    provider = configure_tracing(service_name="test-service")

    assert isinstance(provider, TracerProvider)


def test_configure_tracing_installs_no_span_processor_when_no_endpoint() -> None:
    """FRE-1064 AC-7: with otlp_endpoint=None, no span processor is attached."""
    provider = configure_tracing(service_name="test-service", otlp_endpoint=None)

    processors = provider._active_span_processor._span_processors  # noqa: SLF001
    assert processors == ()


def test_configure_tracing_attaches_otlp_processor_when_endpoint_given() -> None:
    """FRE-1070: given an endpoint, a BatchSpanProcessor exporting OTLP to that endpoint
    is attached — this is the Collector exporter ADR-0129 D5 requires.
    """
    provider = configure_tracing(service_name="test-service", otlp_endpoint="collector-host:4317")

    processors = provider._active_span_processor._span_processors  # noqa: SLF001
    assert len(processors) == 1
    (processor,) = processors
    assert isinstance(processor, BatchSpanProcessor)
    exporter = processor.span_exporter
    assert isinstance(exporter, OTLPSpanExporter)


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
