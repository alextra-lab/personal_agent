"""OpenTelemetry tracer-provider bootstrap (ADR-0129 D4).

Registers a :class:`TracerProvider` as the process-wide OTel tracer provider
at service startup, with no span processor attached — export to a backend is
explicitly out of scope for this change (ADR-0129 D4 / FRE-1064 AC-7). Later
tickets in the ADR-0129 chain (FRE-1070) attach the Collector exporter.
"""

from opentelemetry import trace
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


def configure_tracing(service_name: str = "personal-agent") -> TracerProvider:
    """Create and register a :class:`TracerProvider` with no span processors.

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
            identifying this process in exported spans once export is wired.

    Returns:
        The created :class:`TracerProvider`.
    """
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    trace.set_tracer_provider(provider)
    set_global_textmap(TraceContextTextMapPropagator())
    return provider
