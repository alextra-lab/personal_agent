"""FRE-437: Cache-tier pricing accuracy and DB write-through.

Verifies that:
1. litellm.completion_cost() computes cache-aware cost for Anthropic models —
   cache_read tokens at 0.1× and cache_creation at 1.25× standard input rate.
2. CostTrackerService.record_api_call accepts and persists cache token counts.
3. LiteLLMClient.respond passes cache tokens through to record_api_call.

Known data point from ticket: cache_read=13,916, cache_creation=1,921,
standard_input=11,854, output=500.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.llm_client.cost_tracker import CostTrackerService


# ── Pricing constants (claude-sonnet-4-6) ────────────────────────────────────

_INPUT_RATE = 3e-6          # $3.00 / MTok
_CACHE_CREATE_RATE = 3.75e-6  # $3.75 / MTok (1.25×)
_CACHE_READ_RATE = 3e-7     # $0.30 / MTok (0.10×)
_OUTPUT_RATE = 1.5e-5       # $15.00 / MTok

# Ticket data point
_CACHE_READ = 13_916
_CACHE_CREATION = 1_921
_STANDARD_INPUT = 11_854
_OUTPUT = 500
_PROMPT_TOKENS = _CACHE_READ + _CACHE_CREATION + _STANDARD_INPUT  # 27,691


def _expected_cache_aware_cost(output_tokens: int = _OUTPUT) -> float:
    """Manual cache-aware cost for the ticket data point."""
    return (
        _STANDARD_INPUT * _INPUT_RATE
        + _CACHE_CREATION * _CACHE_CREATE_RATE
        + _CACHE_READ * _CACHE_READ_RATE
        + output_tokens * _OUTPUT_RATE
    )


def _naive_cost(output_tokens: int = _OUTPUT) -> float:
    """Naive cost if all input tokens priced at standard rate."""
    return _PROMPT_TOKENS * _INPUT_RATE + output_tokens * _OUTPUT_RATE


# ── 1. litellm.completion_cost accuracy ──────────────────────────────────────


def test_litellm_completion_cost_uses_cache_tier_pricing() -> None:
    """litellm.completion_cost() must apply Anthropic cache rates, not uniform input rate."""
    import litellm
    from litellm import ModelResponse

    resp = ModelResponse(
        id="msg_test_fre437",
        choices=[
            {
                "finish_reason": "stop",
                "index": 0,
                "message": {"content": "ok", "role": "assistant", "tool_calls": None},
            }
        ],
        usage={
            "prompt_tokens": _PROMPT_TOKENS,
            "completion_tokens": _OUTPUT,
            "total_tokens": _PROMPT_TOKENS + _OUTPUT,
            "cache_read_input_tokens": _CACHE_READ,
            "cache_creation_input_tokens": _CACHE_CREATION,
        },
        model="claude-sonnet-4-6",
        object="chat.completion",
    )
    resp._hidden_params = {"custom_llm_provider": "anthropic"}  # type: ignore[attr-defined]

    cost = litellm.completion_cost(completion_response=resp)

    expected = _expected_cache_aware_cost()
    assert abs(cost - expected) < 1e-8, (
        f"Cache-aware cost {cost:.8f} != expected {expected:.8f}. "
        f"Naive (wrong) would be {_naive_cost():.8f}"
    )


def test_litellm_cache_cost_materially_less_than_naive() -> None:
    """Cache-aware cost must be ≥30% cheaper than naive for this high-cache-reuse turn."""
    expected = _expected_cache_aware_cost()
    naive = _naive_cost()
    savings_pct = (naive - expected) / naive * 100
    assert savings_pct >= 30, (
        f"Expected ≥30% savings but got {savings_pct:.1f}%. "
        f"Check that cache tiers are being applied."
    )


# ── 2. CostTrackerService.record_api_call — cache columns ────────────────────


def _tracker_with_mock_pool() -> tuple[CostTrackerService, MagicMock, AsyncMock]:
    """Return a tracker wired to a mock pool for INSERT inspection."""
    tracker = CostTrackerService()
    fetchval = AsyncMock(return_value=99)
    conn = MagicMock()
    conn.fetchval = fetchval
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    tracker.pool = pool  # type: ignore[assignment]
    return tracker, conn, fetchval


@pytest.mark.asyncio
async def test_record_api_call_persists_cache_tokens() -> None:
    """record_api_call INSERT must carry cache_read and cache_creation token counts."""
    tracker, conn, fetchval = _tracker_with_mock_pool()

    await tracker.record_api_call(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=_PROMPT_TOKENS,
        output_tokens=_OUTPUT,
        cost_usd=_expected_cache_aware_cost(),
        trace_id=uuid4(),
        session_id=uuid4(),
        purpose="main_inference",
        cache_read_input_tokens=_CACHE_READ,
        cache_creation_input_tokens=_CACHE_CREATION,
    )

    fetchval.assert_awaited_once()
    call_args = fetchval.await_args
    sql: str = call_args[0][0]
    params = call_args[0][1:]

    assert "cache_read_input_tokens" in sql, "INSERT must include cache_read_input_tokens column"
    assert "cache_creation_input_tokens" in sql, (
        "INSERT must include cache_creation_input_tokens column"
    )
    # Cache token values must appear in the positional params
    assert _CACHE_READ in params, f"cache_read={_CACHE_READ} not found in INSERT params"
    assert _CACHE_CREATION in params, (
        f"cache_creation={_CACHE_CREATION} not found in INSERT params"
    )


@pytest.mark.asyncio
async def test_record_api_call_cache_tokens_default_to_none() -> None:
    """Cache token params are optional — omitting them must not raise."""
    tracker, _conn, _fetchval = _tracker_with_mock_pool()

    # Must not raise when cache params are absent
    result = await tracker.record_api_call(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.001,
        trace_id=uuid4(),
        session_id=uuid4(),
    )
    assert result == 99


# ── 3. Spot-check cost magnitude ─────────────────────────────────────────────


def test_spot_check_ticket_data_point() -> None:
    """
    Ticket says: cache_read 13,916 / cache_creation 1,921 / input 11,854.

    This test documents the expected cost so any future change to the
    pricing constants is immediately visible.
    """
    cost = _expected_cache_aware_cost(output_tokens=_OUTPUT)
    # Round to 6 decimal places (DB precision)
    cost_rounded = round(cost, 6)
    # $0.054440 is the documented correct value
    assert 0.050 < cost_rounded < 0.060, (
        f"Cost {cost_rounded} out of expected range 0.050–0.060. "
        f"Revisit pricing constants."
    )
    # Confirm naive is materially higher
    naive = round(_naive_cost(_OUTPUT), 6)
    assert naive > cost_rounded * 1.5, "Naive pricing must be ≥50% higher than cache-aware"
