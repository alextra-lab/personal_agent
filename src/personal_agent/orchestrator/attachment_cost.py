"""Attachment-agnostic cloud cost estimation (ADR-0101 §8b / FRE-691).

The pre-flight cost estimate that gates a cloud attachment turn against the
configured confirmation threshold. Deliberately **attachment-type-agnostic**: it
operates on resolved attachment *blocks* and a per-block token estimate, so the
image case (the bounded instance — ``IMAGE_BLOCK_TOKEN_ESTIMATE`` tokens per image,
no page multiplication) and the ADR-0102 document case (page-multiplied token count)
share one estimator. FRE-686 (PDF cost) becomes reuse-plus-PDF-specifics, not a
fresh build — it supplies a larger ``per_block_tokens`` and reuses this function.
"""

from __future__ import annotations

from decimal import Decimal


def estimate_attachment_cloud_cost_usd(
    *,
    block_count: int,
    per_block_tokens: int,
    input_price_per_token: Decimal,
) -> Decimal:
    """Estimate the marginal cloud input cost of a turn's resolved attachment blocks.

    Args:
        block_count: Number of resolved attachment content blocks in the turn.
        per_block_tokens: Estimated input tokens each block contributes (images pass
            ``IMAGE_BLOCK_TOKEN_ESTIMATE``; documents pass a page-multiplied count).
        input_price_per_token: The serving cloud model's USD input-token price. ``0``
            (unpriced / local model) yields a ``0`` estimate that never trips the gate.

    Returns:
        The estimated cost in USD, rounded to 6 decimals (matches ``DECIMAL(10, 6)``).
    """
    total = Decimal(block_count) * Decimal(per_block_tokens) * input_price_per_token
    return total.quantize(Decimal("0.000001"))
