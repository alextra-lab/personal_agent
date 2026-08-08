"""OpenTelemetry tracer-provider bootstrap (ADR-0129 D4/D5).

Registers a :class:`TracerProvider` as the process-wide OTel tracer provider
at service startup. When given an OTLP endpoint, attaches a
:class:`~opentelemetry.sdk.trace.export.BatchSpanProcessor` exporting to the
OTel Collector — the single trace egress point (ADR-0129 D5, FRE-1070). With
no endpoint, no span processor is attached (FRE-1064 AC-7's original scope,
kept for tests that need a provider with no export path).
"""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


def configure_tracing(
    service_name: str = "personal-agent", otlp_endpoint: str | None = None
) -> TracerProvider:
    """Create and register a :class:`TracerProvider`, optionally exporting via OTLP.

    Registers the provider as OpenTelemetry's process-wide tracer provider via
    :func:`opentelemetry.trace.set_tracer_provider`, which only takes effect on
    the first call in a process — later calls in the same process return a
    fresh, usable provider without becoming the global one.

    Also sets the global text-map propagator to W3C ``tracecontext``
    explicitly (ADR-0129 D1: explicit propagation over implicit convention;
    FRE-1067 AC-14 depends on ``opentelemetry.propagate.inject`` emitting a
    ``traceparent`` header) rather than relying on the SDK's own default.

    Args:
        service_name: Value for the ``service.name`` resource attribute,
            identifying this process in exported spans.
        otlp_endpoint: OTLP gRPC endpoint of the Collector (ADR-0129 D5). The
            production call site (``service.app``) always supplies this from
            ``settings.otel_exporter_endpoint``; ``None`` is for tests that
            need a provider with no export path.

    Returns:
        The created :class:`TracerProvider`.
    """
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    trace.set_tracer_provider(provider)
    set_global_textmap(TraceContextTextMapPropagator())
    if otlp_endpoint is not None:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True))
        )
    return provider
