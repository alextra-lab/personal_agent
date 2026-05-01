"""Pure-function tests for cost_estimator (FRE-305).

The estimator is unit-test territory — no DB, no LLM. We test the math:
``reservation = input_cost + min(max_tokens, default) × output_price × safety``.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from personal_agent.cost_gate import (
    BudgetConfig,
    CapEntry,
    OnDenialBehaviour,
    RoleConfig,
)
from personal_agent.llm_client.cost_estimator import (
    estimate_reservation,
    estimate_reservation_for_call,
)


def _config(default_output_tokens: int = 1024, safety_factor: float = 1.2) -> BudgetConfig:
    return BudgetConfig(
        version=1,
        roles={
            "main_inference": RoleConfig(
                default_output_tokens=default_output_tokens,
                safety_factor=safety_factor,
                on_denial=OnDenialBehaviour.RAISE,
            ),
        },
        caps=[CapEntry(time_window="weekly", role="_total", cap_usd=Decimal("25.00"))],
    )


def test_estimator_basic_math() -> None:
    """input_cost + (default_output × output_price × safety) for max_tokens=None."""
    # 100 input × $0.0001 = $0.01
    # 1024 output × $0.0002 × 1.2 = $0.245760
    # total: $0.255760
    reservation = estimate_reservation(
        role="main_inference",
        input_tokens=100,
        max_tokens=None,
        input_price_per_token=Decimal("0.0001"),
        output_price_per_token=Decimal("0.0002"),
        config=_config(),
    )
    assert reservation == Decimal("0.255760")


def test_max_tokens_caps_output_when_smaller_than_default() -> None:
    """When max_tokens < default_output_tokens, the smaller value wins."""
    # 100 input × $0.0001 = $0.01
    # min(256, 1024)=256 output × $0.0002 × 1.2 = $0.061440
    # total: $0.071440
    reservation = estimate_reservation(
        role="main_inference",
        input_tokens=100,
        max_tokens=256,
        input_price_per_token=Decimal("0.0001"),
        output_price_per_token=Decimal("0.0002"),
        config=_config(default_output_tokens=1024),
    )
    assert reservation == Decimal("0.071440")


def test_max_tokens_larger_than_default_uses_default() -> None:
    """When max_tokens > default_output_tokens, default wins (we trust budget.yaml)."""
    # min(8192, 1024) = 1024
    # 0 input + 1024 × 0.0002 × 1.2 = $0.245760
    reservation = estimate_reservation(
        role="main_inference",
        input_tokens=0,
        max_tokens=8192,
        input_price_per_token=Decimal("0"),
        output_price_per_token=Decimal("0.0002"),
        config=_config(default_output_tokens=1024),
    )
    assert reservation == Decimal("0.245760")


def test_safety_factor_applied_to_output_only() -> None:
    """Safety factor multiplies the output cost, not the input."""
    # 1000 input × $0.001 = $1.00
    # 100 output × $0.001 × 1.5 = $0.15
    # total: $1.15 (input untouched by safety_factor)
    reservation = estimate_reservation(
        role="main_inference",
        input_tokens=1000,
        max_tokens=100,
        input_price_per_token=Decimal("0.001"),
        output_price_per_token=Decimal("0.001"),
        config=_config(default_output_tokens=512, safety_factor=1.5),
    )
    assert reservation == Decimal("1.150000")


def test_zero_pricing_yields_zero_reservation() -> None:
    """Local / unpriced models reserve $0 — gate logs warning but doesn't deny."""
    reservation = estimate_reservation(
        role="main_inference",
        input_tokens=10000,
        max_tokens=8192,
        input_price_per_token=Decimal("0"),
        output_price_per_token=Decimal("0"),
        config=_config(),
    )
    assert reservation == Decimal("0")


def test_undeclared_role_raises_key_error() -> None:
    """Misspelled or undeclared role surfaces as KeyError, not silent default."""
    with pytest.raises(KeyError):
        estimate_reservation(
            role="not_a_real_role",
            input_tokens=1,
            max_tokens=1,
            input_price_per_token=Decimal("1"),
            output_price_per_token=Decimal("1"),
            config=_config(),
        )


def test_six_decimal_rounding() -> None:
    """Result is quantised to 6 decimals to match DECIMAL(10, 6) in DB."""
    # An input that would compute to more than 6 decimals naturally.
    reservation = estimate_reservation(
        role="main_inference",
        input_tokens=7,
        max_tokens=11,
        input_price_per_token=Decimal("0.000003"),
        output_price_per_token=Decimal("0.0000007"),
        config=_config(default_output_tokens=512, safety_factor=1.2),
    )
    # 7 × 0.000003 = 0.000021
    # 11 × 0.0000007 × 1.2 = 0.00000924
    # total: 0.00003024  → quantised to 0.000030
    assert reservation == Decimal("0.000030")


# ---------------------------------------------------------------------------
# Pricing-lookup fallback (regression for the openai/gpt-5.4-nano
# pricing-unknown case observed in prod logs at 2026-05-01 06:46:39).
# ---------------------------------------------------------------------------


def test_estimate_for_call_falls_back_to_bare_model_name() -> None:
    """LiteLLM indexes some models by bare name, others by ``provider/name``.

    The estimator must try both forms, otherwise calls to (e.g.)
    ``openai/gpt-5.4-nano`` reserve $0 because litellm has it as just
    ``gpt-5.4-nano`` — silently bypassing the cap on cost-based denial.
    """
    # Stub registry — only the bare name is present.
    fake_registry = {
        "gpt-5.4-nano": {
            "input_cost_per_token": 2e-07,
            "output_cost_per_token": 12.5e-07,
        },
    }

    with (
        patch("litellm.model_cost", new=fake_registry),
        patch("litellm.token_counter", return_value=100),
    ):
        reservation = estimate_reservation_for_call(
            role="main_inference",
            model="openai/gpt-5.4-nano",  # prefixed form — lookup must strip prefix
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=128,
            config=_config(default_output_tokens=128, safety_factor=1.2),
        )
    # 100 × 2e-7 = 2e-5
    # min(128, 128)=128 × 12.5e-7 × 1.2 = 1.92e-4
    # total ≈ 2.12e-4
    assert reservation > Decimal("0"), "lookup must fall back to the bare model name"
    assert reservation == Decimal("0.000212")


def test_estimate_for_call_pricing_truly_unknown_returns_zero() -> None:
    """When litellm has *no* entry under either form, fall through to $0."""
    with (
        patch("litellm.model_cost", new={}),
        patch("litellm.token_counter", return_value=100),
    ):
        reservation = estimate_reservation_for_call(
            role="main_inference",
            model="openai/some-future-model",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=128,
            config=_config(default_output_tokens=128, safety_factor=1.2),
        )
    # No pricing → 100 × 0 + 128 × 0 × 1.2 = 0
    assert reservation == Decimal("0")
