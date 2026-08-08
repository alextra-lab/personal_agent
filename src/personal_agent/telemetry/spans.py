"""Span-tree helpers for ADR-0129 D3 (root -> step -> {model-call, tool-call}).

Every span-creating function accepts an optional ``tracer`` parameter so tests
can inject a locally-scoped :class:`~opentelemetry.trace.Tracer` bound to their
own :class:`TracerProvider` + ``InMemorySpanExporter`` — matching the pattern
:class:`~personal_agent.telemetry.otel_middleware.RequestRootSpanMiddleware`
already established, rather than mutating the process-global provider.

``open_step_span``/``close_step_span`` use OTel's own context attach/detach
so the step span is genuinely the *current* span for its whole lifetime.
``model_call_span``/``tool_call_span`` are plain ``start_as_current_span``
context managers and need no explicit parent — they inherit the step span
automatically because it is current.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from contextvars import Token
from importlib.metadata import version as _pkg_version

from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.context.context import Context
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as gen_ai
from opentelemetry.trace import Span, Tracer

_TRACER_NAME = "personal_agent"
_ATTR_NAMESPACE = "personal_agent"

# AC-13: pinned exactly (not a floor) so a pyproject.toml bump that moves past
# this value fails the AC-13 test loudly instead of drifting silently. Bump
# this constant in the same commit as the pyproject.toml dependency bump.
SEMCONV_VERSION = "0.65b0"


def get_tracer() -> Tracer:
    """Return this project's named tracer, resolved from the global provider."""
    return trace.get_tracer(_TRACER_NAME)


def namespaced(key: str) -> str:
    """Prefix a non-semconv attribute key with the project namespace (AC-8)."""
    return f"{_ATTR_NAMESPACE}.{key}"


def open_step_span(*, iteration: int, tracer: Tracer | None = None) -> tuple[Span, Token[Context]]:
    """Start the step span and attach it as the current OTel context.

    Returns ``(span, token)``; the caller MUST pass both to
    :func:`close_step_span` — the token is what makes ``context.detach``
    restore the prior context correctly rather than clobbering it.
    """
    span = (tracer or get_tracer()).start_span(
        "step", attributes={namespaced("step.iteration"): iteration}
    )
    token = context_api.attach(trace.set_span_in_context(span))
    return span, token


def close_step_span(span: Span, token: Token[Context], *, tool_count: int) -> None:
    """End the step span and detach it, restoring the prior OTel context."""
    span.set_attribute(namespaced("step.tool_count"), tool_count)
    span.end()
    context_api.detach(token)


def model_call_span(
    *, role: str, model: str, provider: str, tracer: Tracer | None = None
) -> AbstractContextManager[Span]:
    """Context manager: opens a model-call span as CURRENT.

    Parent is whatever span is current (the step span, via
    ``open_step_span``'s attach) — no explicit ``context=`` needed.
    """
    return (tracer or get_tracer()).start_as_current_span(
        f"model_call {model}",
        attributes={
            gen_ai.GEN_AI_OPERATION_NAME: role,  # FRE-1037 purpose vocabulary (AC-7)
            gen_ai.GEN_AI_SYSTEM: provider,
            gen_ai.GEN_AI_REQUEST_MODEL: model,
        },
    )


def tool_call_span(*, tool_name: str, tracer: Tracer | None = None) -> AbstractContextManager[Span]:
    """Context manager: opens a tool-call span as CURRENT, sibling to model-call spans."""
    return (tracer or get_tracer()).start_as_current_span(
        f"tool_call {tool_name}", attributes={namespaced("tool.name"): tool_name}
    )


def assert_semconv_version_pinned() -> None:
    """AC-13: the pinned constant above must equal what's actually installed."""
    installed = _pkg_version("opentelemetry-semantic-conventions")
    if installed != SEMCONV_VERSION:
        raise RuntimeError(
            f"SEMCONV_VERSION={SEMCONV_VERSION!r} does not match installed "
            f"opentelemetry-semantic-conventions=={installed!r}; bump both together."
        )
