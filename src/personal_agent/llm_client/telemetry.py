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

FRE-376 Phase 3 (this revision): the back-compat aliases (``model_id``,
``prompt_tokens``, ``completion_tokens``, ``tokens``, ``cache_write_tokens``)
and the legacy ``litellm_request_*`` event names have been removed. Consumers
must read the canonical names (``model``, ``input_tokens``, ``output_tokens``,
``total_tokens``, ``cache_creation_input_tokens``) and filter on
``event:model_call_completed`` / ``event:model_call_started``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from personal_agent.telemetry.events import MODEL_CALL_COMPLETED, MODEL_CALL_STARTED

if TYPE_CHECKING:
    import structlog.stdlib

    from personal_agent.llm_client.prompt_identity import PromptIdentity
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
    provider: str,
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
        provider: ADR-0121 catalog provider key (e.g. ``"anthropic"``,
            ``"slm_local"``) — the dimension cost/telemetry records are
            attributed against (ADR-0121 §8, replacing the retired
            ``TraceContext.profile``).
        trace_ctx: Trace context as passed into the call (pre-``new_span``).
        span_id: Newly minted span id for this model call.
        extra: Provider-specific fields to merge into the emit
            (e.g. ``{"budget_role": ..., "reservation_amount": ...}``).
    """
    payload: dict[str, Any] = {
        "model": model,
        "provider": provider,
        "role": role,
        "endpoint": endpoint,
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
    provider: str,
    trace_ctx: TraceContext,
    span_id: str,
    latency_ms: int,
    input_tokens: int | None,
    output_tokens: int | None,
    prompt_identity: PromptIdentity,
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
        provider: ADR-0121 catalog provider key (e.g. ``"anthropic"``,
            ``"slm_local"``) — the dimension cost/telemetry records are
            attributed against (ADR-0121 §8, replacing the retired
            ``TraceContext.profile``).
        trace_ctx: Trace context as passed into the call (pre-``new_span``).
        span_id: Span id of the model call (same one used in ``_started``).
        latency_ms: Wall-clock latency of the call in milliseconds.
        input_tokens: Prompt token count, when reported by the provider.
        output_tokens: Completion token count, when reported by the provider.
        prompt_identity: Identity of the prompt sent on this call (ADR-0078 D1/D4).
            Flattened into ``prompt_callsite`` / ``prompt_component_ids`` /
            ``prompt_static_prefix_hash`` / ``prompt_dynamic_hash``. Callers that
            lack a named composition derive a fallback so this is never absent.
        total_tokens: Provider-reported total (may differ from input+output
            when cache or reasoning tokens are counted separately).
        cache_read_tokens: Provider-specific cache-read token count.
        extra: Provider-specific fields to merge into the emit
            (e.g. ``{"api_type": ..., "fallback_used": ..., "cost_usd": ...}``).
    """
    payload: dict[str, Any] = {
        "model": model,
        "provider": provider,
        "role": role,
        "endpoint": endpoint,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": cache_read_tokens,
        "prompt_callsite": prompt_identity.callsite,
        "prompt_component_ids": list(prompt_identity.component_ids),
        "prompt_static_prefix_hash": prompt_identity.static_prefix_hash,
        "prompt_dynamic_hash": prompt_identity.dynamic_hash,
        **_identity_fields(trace_ctx=trace_ctx, span_id=span_id),
    }
    if extra:
        payload.update(extra)
    log.info(MODEL_CALL_COMPLETED, **payload)
