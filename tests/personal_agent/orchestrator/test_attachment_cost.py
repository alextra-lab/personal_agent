"""FRE-691 / ADR-0101 §8b: attachment-agnostic cloud cost estimator."""

from __future__ import annotations

from decimal import Decimal

import pytest

from personal_agent.orchestrator.attachment_cost import estimate_attachment_cloud_cost_usd

_SONNET_INPUT_PRICE = Decimal("0.000003")  # $3/MTok


def test_image_turn_estimate_math() -> None:
    """4 images × 1600 tokens × $3/MTok = $0.0192 (the bounded image instance)."""
    cost = estimate_attachment_cloud_cost_usd(
        block_count=4, per_block_tokens=1600, input_price_per_token=_SONNET_INPUT_PRICE
    )
    assert cost == pytest.approx(Decimal("0.0192"), rel=1e-6)


def test_estimator_is_attachment_agnostic() -> None:
    """Page-multiplied tokens (the ADR-0102 PDF case) scale the cost proportionally.

    The estimator operates on generic (block_count, per_block_tokens): the image case
    passes 1600; the document case passes a page-multiplied token count and reuses this
    same math rather than a fresh estimator (reuse-plus-PDF-specifics, FRE-686).
    """
    image = estimate_attachment_cloud_cost_usd(
        block_count=1, per_block_tokens=1600, input_price_per_token=_SONNET_INPUT_PRICE
    )
    doc_10_pages = estimate_attachment_cloud_cost_usd(
        block_count=1, per_block_tokens=1600 * 10, input_price_per_token=_SONNET_INPUT_PRICE
    )
    assert doc_10_pages == pytest.approx(image * 10, rel=1e-6)


def test_zero_price_yields_zero() -> None:
    """An unpriced model (input_price 0) never trips the threshold."""
    cost = estimate_attachment_cloud_cost_usd(
        block_count=8, per_block_tokens=1600, input_price_per_token=Decimal("0")
    )
    assert cost == Decimal("0")
