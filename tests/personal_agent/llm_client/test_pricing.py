"""FRE-691 / ADR-0101 §8b AC-11: config-owned cloud pricing + non-zero metering.

These tests assert the *outcome* — that after registering our config pricing,
the commit-cost path reconciles a non-zero cost whose token basis includes image
tokens — rather than asserting a specific ``litellm.model_cost`` dict key (litellm
folds a prefixed registration onto the bare key).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from litellm import ModelResponse, Usage

from personal_agent.llm_client.cost_estimator import actual_cost_for_response
from personal_agent.llm_client.models import ModelConfig, ModelDefinition
from personal_agent.llm_client.pricing import register_model_pricing

# A pricing pair distinct from litellm's shipped values so the assertions prove
# *our* config drove the number, not litellm's coincidental registry entry.
_INPUT_PRICE = 0.000009
_OUTPUT_PRICE = 0.000045
_MODEL = "anthropic/claude-sonnet-4-6"


def _priced_config() -> ModelConfig:
    """A ModelConfig with one priced cloud Claude entry."""
    return ModelConfig(
        models={
            "claude_sonnet": ModelDefinition(
                id="claude-sonnet-4-6",
                provider="anthropic",
                provider_type="cloud",
                max_tokens=32768,
                context_length=200000,
                max_concurrency=10,
                default_timeout=180,
                supports_vision=True,
                input_cost_per_token=_INPUT_PRICE,
                output_cost_per_token=_OUTPUT_PRICE,
            )
        },
        entity_extraction_role="claude_sonnet",
        captains_log_role="claude_sonnet",
        insights_role="claude_sonnet",
    )


def _response(model: str, prompt_tokens: int, completion_tokens: int) -> ModelResponse:
    return ModelResponse(
        model=model,
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


def test_register_model_pricing_makes_commit_use_our_price() -> None:
    """AC-11(a): the model definition's pricing drives the reconciled cost."""
    count = register_model_pricing(_priced_config())
    assert count == 1

    cost = actual_cost_for_response(
        response=_response("claude-sonnet-4-6", 1000, 500), model=_MODEL
    )
    expected = Decimal(1000) * Decimal(str(_INPUT_PRICE)) + Decimal(500) * Decimal(
        str(_OUTPUT_PRICE)
    )
    assert cost == pytest.approx(expected, rel=1e-6)
    assert cost > 0


def test_cloud_image_turn_commits_nonzero_with_image_basis() -> None:
    """AC-11(b): committed cost is non-zero and scales with image tokens.

    The image tokens land in ``usage.prompt_tokens`` (as Anthropic counts them),
    so a turn carrying an image commits strictly more than a text-only turn — the
    committed basis is not text-only.
    """
    register_model_pricing(_priced_config())

    cost_text = actual_cost_for_response(
        response=_response("claude-sonnet-4-6", 200, 50), model=_MODEL
    )
    cost_image = actual_cost_for_response(
        response=_response("claude-sonnet-4-6", 200 + 1600, 50), model=_MODEL
    )

    assert cost_image > 0
    # The +1600 image tokens add exactly 1600 × input price to the committed cost.
    assert cost_image - cost_text == pytest.approx(
        Decimal(1600) * Decimal(str(_INPUT_PRICE)), rel=1e-6
    )


def test_dated_response_model_still_commits_nonzero() -> None:
    """Codex High-2: an unmapped dated response id must not silently commit $0.

    litellm.completion_cost raises when it must derive an unknown dated id from the
    response; passing the known request model (plus the config-pricing fallback)
    keeps the committed cost non-zero with an image-inclusive basis.
    """
    register_model_pricing(_priced_config())

    cost = actual_cost_for_response(
        response=_response("claude-sonnet-4-6-20990101", 1800, 100), model=_MODEL
    )
    expected = Decimal(1800) * Decimal(str(_INPUT_PRICE)) + Decimal(100) * Decimal(
        str(_OUTPUT_PRICE)
    )
    assert cost == pytest.approx(expected, rel=1e-6)
    assert cost > 0
