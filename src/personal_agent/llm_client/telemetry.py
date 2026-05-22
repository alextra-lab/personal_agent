"""Canonical telemetry emit helpers for model clients (ADR-0074 §I2).

Both :class:`personal_agent.llm_client.client.LocalLLMClient` and
:class:`personal_agent.llm_client.litellm_client.LiteLLMClient` route their
``model_call_started`` / ``model_call_completed`` emissions through this
module so a request handler that switches between local and cloud paths
cannot tell the difference from telemetry alone.

The canonical field contract is enumerated in
:data:`personal_agent.telemetry.events.CANONICAL_MODEL_CALL_STARTED_FIELDS` and
:data:`personal_agent.telemetry.events.CANONICAL_MODEL_CALL_COMPLETED_FIELDS`.
The Phase 2 parity test imports those frozensets directly and asserts that
both clients emit a superset — adding a new required field there forces
every model client to populate it.

Back-compat aliases (``model_id``, ``prompt_tokens``, ``completion_tokens``,
the legacy ``litellm_request_*`` event names) are written here as
double-emits. They will be removed in Phase 3 cleanup once downstream Kibana
dashboards and reflection queries are migrated to the canonical names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from personal_agent.telemetry.events import MODEL_CALL_COMPLETED, MODEL_CALL_STARTED

if TYPE_CHECKING:
    import structlog.stdlib

    from personal_agent.telemetry.trace import TraceContext


def _identity_fields(*, trace_ctx: TraceContext, span_id: str) -> dict[str, Any]:
    """Return the canonical identity tuple for an emit.

    The ``parent_span_id`` of the emit is the calling context's *current*
    span — by codebase convention :attr:`TraceContext.parent_span_id` is the
    span this context inhabits, so a child operation's parent is its caller's
    ``parent_span_id``. The caller must pass ``trace_ctx`` *as it existed
    before* ``new_span()`` for this to be correct.

    Args:
        trace_ctx: Trace context as passed into the model call (pre-``new_span``).
        span_id: Freshly minted span id for the model call itself.

    Returns:
        A dict with ``trace_id``, ``session_id``, ``span_id``, ``parent_span_id``.
    """
    return {
        "trace_id": trace_ctx.trace_id,
        "session_id": trace_ctx.session_id,
        "span_id": span_id,
        "parent_span_id": trace_ctx.parent_span_id,
    }


def emit_model_call_started(
    *,
    log: structlog.stdlib.BoundLogger,
    role: str,
    model: str,
    endpoint: str,
    trace_ctx: TraceContext,
    span_id: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit the canonical ``model_call_started`` event (ADR-0074 §I2).

    Args:
        log: Bound structlog logger from the calling client module.
        role: ``ModelRole.value`` for the call (``"primary"`` / ``"extractor"`` / …).
        model: Canonical model identifier (e.g. ``"anthropic/claude-sonnet-4-6"``).
        endpoint: URL or provider tag this call dispatches to.
        trace_ctx: Trace context as passed into the call (pre-``new_span``).
        span_id: Newly minted span id for this model call.
        extra: Provider-specific fields to merge into the emit
            (e.g. ``{"budget_role": ..., "reservation_amount": ...}``).
    """
    payload: dict[str, Any] = {
        "model": model,
        "role": role,
        "endpoint": endpoint,
        # Back-compat alias for one release cycle (Phase 3 removes).
        "model_id": model,
        **_identity_fields(trace_ctx=trace_ctx, span_id=span_id),
    }
    if extra:
        payload.update(extra)
    log.info(MODEL_CALL_STARTED, **payload)


def emit_model_call_completed(
    *,
    log: structlog.stdlib.BoundLogger,
    role: str,
    model: str,
    endpoint: str,
    trace_ctx: TraceContext,
    span_id: str,
    latency_ms: int,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit the canonical ``model_call_completed`` event (ADR-0074 §I2).

    Args:
        log: Bound structlog logger from the calling client module.
        role: ``ModelRole.value`` for the call.
        model: Canonical model identifier.
        endpoint: URL or provider tag this call dispatched to.
        trace_ctx: Trace context as passed into the call (pre-``new_span``).
        span_id: Span id of the model call (same one used in ``_started``).
        latency_ms: Wall-clock latency of the call in milliseconds.
        input_tokens: Prompt token count, when reported by the provider.
        output_tokens: Completion token count, when reported by the provider.
        total_tokens: Provider-reported total (may differ from input+output
            when cache or reasoning tokens are counted separately).
        cache_read_tokens: Provider-specific cache-read token count.
        extra: Provider-specific fields to merge into the emit
            (e.g. ``{"api_type": ..., "fallback_used": ..., "cost_usd": ...}``).
    """
    payload: dict[str, Any] = {
        "model": model,
        "role": role,
        "endpoint": endpoint,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": cache_read_tokens,
        # Back-compat aliases for one release cycle (Phase 3 removes).
        "model_id": model,
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        **_identity_fields(trace_ctx=trace_ctx, span_id=span_id),
    }
    if extra:
        payload.update(extra)
    log.info(MODEL_CALL_COMPLETED, **payload)


def emit_legacy_litellm_start(
    *,
    log: structlog.stdlib.BoundLogger,
    role: str,
    model: str,
    trace_ctx: TraceContext,
    budget_role: str,
    reservation_amount: str,
    max_tokens: int | None,
) -> None:
    """Emit the deprecated ``litellm_request_start`` event for back-compat.

    Kept alongside :func:`emit_model_call_started` so Kibana dashboards and
    queries that currently filter on ``event:litellm_request_start`` keep
    working through one release cycle. Phase 3 will drop this once consumers
    migrate to the canonical event name.
    """
    log.info(
        "litellm_request_start",
        model=model,
        trace_id=trace_ctx.trace_id,
        session_id=trace_ctx.session_id,
        role=role,
        budget_role=budget_role,
        reservation_amount=reservation_amount,
        max_tokens=max_tokens,
    )


def emit_legacy_litellm_complete(
    *,
    log: structlog.stdlib.BoundLogger,
    role: str,
    model: str,
    endpoint: str,
    trace_ctx: TraceContext,
    latency_ms: int,
    elapsed_s: float,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    cost_usd: float | None,
    tool_calls: int,
    cache_read_tokens: int | None,
    cache_creation_input_tokens: int | None,
) -> None:
    """Emit the deprecated ``litellm_request_complete`` event for back-compat.

    Mirrors the field shape that was previously emitted inline in
    :meth:`LiteLLMClient.respond`. Kept until Phase 3 cleanup migrates
    consumers to ``model_call_completed``.
    """
    log.info(
        "litellm_request_complete",
        model=model,
        trace_id=trace_ctx.trace_id,
        session_id=trace_ctx.session_id,
        role=role,
        endpoint=endpoint,
        latency_ms=latency_ms,
        elapsed_s=elapsed_s,
        completion_tokens=output_tokens,
        prompt_tokens=input_tokens,
        total_tokens=total_tokens,
        tokens=total_tokens,
        cost_usd=cost_usd,
        tool_calls=tool_calls,
        cache_read_tokens=cache_read_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_write_tokens=cache_creation_input_tokens,
    )
