"""Pre-call cost estimation for the Cost Check Gate (ADR-0065 D3 / FRE-305).

The gate's reservation amount is sized to:

    reservation = exact_input_cost
                + min(max_tokens, default_output_tokens) × output_price × safety_factor

where ``default_output_tokens`` and ``safety_factor`` come from the
caller's role config in ``budget.yaml``. The safety factor (1.2× by default)
catches 95%+ of overshoots; the post-call ``commit`` settles the difference
between estimate and actual on the counter.

Pricing is sourced from ``litellm.model_cost`` — the same registry the
existing post-call ``litellm.completion_cost()`` already uses. Token counts
are sourced from ``litellm.token_counter`` (provider-aware tokenizer).
Both calls fall back gracefully on unknown models so an unexpected model id
doesn't crash the call path; in that case the reservation is computed from
``max_tokens`` worth of unknown-priced tokens (treated as zero — the gate
still records the reservation for audit but won't deny based on cost).

**Cache-tier pricing note (FRE-437):** Anthropic prompt caching has asymmetric
per-token rates (cache_read ≈ 0.10× standard; cache_creation ≈ 1.25× standard).
Pre-call, the KV-cache state on the server is unknown, so the reservation uses
the uniform ``input_cost_per_token`` rate for all input tokens — this
over-reserves on cache-heavy turns (by up to ~66% for high-reuse sessions, per
FRE-437 analysis). The over-reservation is harmless: ``gate.commit()`` settles
the actual cost computed by ``litellm.completion_cost()``, which correctly reads
``cache_read_input_tokens`` / ``cache_creation_input_tokens`` from the response
and applies the discounted/premium rates from ``litellm.model_cost``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import structlog

from personal_agent.cost_gate import BudgetConfig, RoleConfig

log = structlog.get_logger(__name__)


def estimate_reservation(
    *,
    role: str,
    input_tokens: int,
    max_tokens: int | None,
    input_price_per_token: Decimal,
    output_price_per_token: Decimal,
    config: BudgetConfig,
) -> Decimal:
    """Compute the reservation amount in USD for a single LLM call.

    Args:
        role: Budget role identifier (must be declared in ``config.roles``).
        input_tokens: Exact input-token count for the call (from
            ``litellm.token_counter`` or equivalent).
        max_tokens: Caller's ``max_tokens`` cap, or ``None`` if not pinned.
        input_price_per_token: USD per input token (from
            ``litellm.model_cost[model]['input_cost_per_token']``).
        output_price_per_token: USD per output token.
        config: Loaded ``BudgetConfig``; used to look up
            ``default_output_tokens`` and ``safety_factor`` for ``role``.

    Returns:
        The reservation amount, rounded to 6 decimal places to match the
        DB ``DECIMAL(10, 6)`` precision.

    Raises:
        KeyError: If ``role`` is not declared in ``config.roles``. Callers
            should treat this as a configuration error — silently defaulting
            would let an under-specified role bypass any cap that doesn't
            apply by name.
    """
    role_cfg: RoleConfig = config.role(role)

    output_tokens = role_cfg.default_output_tokens
    if max_tokens is not None:
        output_tokens = min(max_tokens, role_cfg.default_output_tokens)

    input_cost = Decimal(input_tokens) * input_price_per_token
    output_cost = (
        Decimal(output_tokens) * output_price_per_token * Decimal(str(role_cfg.safety_factor))
    )

    total = (input_cost + output_cost).quantize(Decimal("0.000001"))
    return total


def estimate_reservation_for_call(
    *,
    role: str,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    max_tokens: int | None,
    config: BudgetConfig,
    trace_id: str | None = None,
) -> Decimal:
    """Convenience wrapper that pulls token count + pricing from litellm.

    Most callers want this rather than ``estimate_reservation`` directly:
    it handles the ``litellm.token_counter`` and ``litellm.model_cost``
    lookups (with graceful fallback when the model isn't in the registry).

    Args:
        role: Budget role identifier.
        model: LiteLLM model string (e.g. ``"anthropic/claude-sonnet-4-6"``).
        messages: OpenAI-format messages the call will send.
        max_tokens: Caller's ``max_tokens`` cap.
        config: Loaded ``BudgetConfig``.
        trace_id: Originating request trace_id, threaded onto warning logs for
            §I3 identity threading. Defaults to ``None`` for callers without
            a request context (rare — all production call paths have one).

    Returns:
        Reservation amount in USD.
    """
    import litellm  # noqa: PLC0415 — keep import out of module load

    try:
        input_tokens = int(litellm.token_counter(model=model, messages=list(messages)))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "cost_estimator_token_counter_failed",
            model=model,
            error=str(exc),
            trace_id=trace_id,
        )
        # Fall back to a generous default; the gate's safety_factor still
        # guards against runaway estimates.
        input_tokens = 4096

    # litellm.model_cost indexes some models by the prefixed form
    # (``anthropic/claude-…``) and others by the bare id (``gpt-5.4-nano``).
    # Try both rather than silently fall back to $0 — every miss here means
    # the gate cannot deny on cost for that model.
    model_cost_table: dict[str, Any] = getattr(litellm, "model_cost", {})
    pricing: dict[str, Any] = model_cost_table.get(model) or model_cost_table.get(
        model.rsplit("/", 1)[-1], {}
    )
    input_price = Decimal(str(pricing.get("input_cost_per_token", "0")))
    output_price = Decimal(str(pricing.get("output_cost_per_token", "0")))

    if input_price == 0 and output_price == 0:
        log.warning(
            "cost_estimator_pricing_unknown",
            model=model,
            note="reservation will be 0; gate cannot deny on cost",
            trace_id=trace_id,
        )

    return estimate_reservation(
        role=role,
        input_tokens=input_tokens,
        max_tokens=max_tokens,
        input_price_per_token=input_price,
        output_price_per_token=output_price,
        config=config,
    )


def actual_cost_for_response(
    *,
    response: Any,
    model: str,
    trace_id: str | None = None,
) -> Decimal:
    """Reconcile the committed USD cost of a completed call, guarding against $0.

    ``litellm.completion_cost`` derives the model from the response when not passed
    one; a *dated* provider id (e.g. ``claude-sonnet-4-6-20990101``) the registry
    does not know raises, which the caller would otherwise swallow as ``$0`` —
    silently metering cloud vision as free (ADR-0101 §8b AC-11, codex High-2). This
    passes the known request ``model`` so litellm prices the registered rate, and on
    any failure (or a ``0`` result while the response carries usage) falls back to
    config pricing × actual usage. ``usage.prompt_tokens`` already includes image
    tokens as the provider counts them, so the committed basis includes images.

    Args:
        response: The litellm response object (carries ``.usage`` + ``.model``).
        model: The request's litellm model string (e.g. ``anthropic/claude-sonnet-4-6``),
            registered via :func:`personal_agent.llm_client.pricing.register_model_pricing`.
        trace_id: Originating request trace_id, threaded onto fallback logs
            (ADR-0074 §I3).

    Returns:
        Committed cost in USD, rounded to 6 decimals (matches ``DECIMAL(10, 6)``).
    """
    import litellm  # noqa: PLC0415 — keep litellm import off module load

    try:
        cost = float(litellm.completion_cost(completion_response=response, model=model))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "actual_cost_completion_cost_failed",
            model=model,
            error=str(exc),
            trace_id=trace_id,
        )
        cost = 0.0

    if cost > 0:
        return Decimal(str(cost)).quantize(Decimal("0.000001"))

    # Fallback: price the actual usage from the (config-registered) registry so a
    # response whose model id litellm can't map still meters non-zero.
    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    if prompt_tokens == 0 and completion_tokens == 0:
        return Decimal("0")

    model_cost_table: dict[str, Any] = getattr(litellm, "model_cost", {})
    pricing: dict[str, Any] = model_cost_table.get(model) or model_cost_table.get(
        model.rsplit("/", 1)[-1], {}
    )
    input_price = Decimal(str(pricing.get("input_cost_per_token", "0")))
    output_price = Decimal(str(pricing.get("output_cost_per_token", "0")))
    fallback = Decimal(prompt_tokens) * input_price + Decimal(completion_tokens) * output_price

    if fallback > 0:
        log.info(
            "actual_cost_fallback_priced",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=float(fallback),
            trace_id=trace_id,
        )
    else:
        log.warning(
            "actual_cost_unpriced",
            model=model,
            note="committed as $0; model has no registered pricing",
            trace_id=trace_id,
        )
    return fallback.quantize(Decimal("0.000001"))
