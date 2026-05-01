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
        )

    return estimate_reservation(
        role=role,
        input_tokens=input_tokens,
        max_tokens=max_tokens,
        input_price_per_token=input_price,
        output_price_per_token=output_price,
        config=config,
    )
