"""FRE-974 — record_vendor_cost: identity-gated cost recording for non-chat vendor calls.

Covers the OVH-embedding / Voyage-reranker cost path, which has more varied
identity availability than the strict LLM path (CostTracker.record_api_call
raises MissingIdentityError on a missing trace_id/session_id) — this helper
never raises, so a cost-recording failure can never break an embedding or
rerank call.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.llm_client.cost_tracker import SYSTEM_SESSION_ID, record_vendor_cost


def _mock_cost_tracker_service() -> tuple[MagicMock, AsyncMock]:
    """Return a mock CostTrackerService instance and its record_api_call mock."""
    instance = MagicMock()
    instance.connect = AsyncMock()
    instance.disconnect = AsyncMock()
    instance.record_api_call = AsyncMock(return_value=1)
    return instance, instance.record_api_call


@pytest.mark.asyncio
async def test_records_on_valid_identity() -> None:
    """Valid UUID trace_id/session_id -> record_api_call is invoked with them."""
    instance, record_api_call = _mock_cost_tracker_service()
    trace_id = str(uuid4())
    session_id = str(uuid4())

    with patch("personal_agent.llm_client.cost_tracker.CostTrackerService", return_value=instance):
        await record_vendor_cost(
            provider="voyage",
            model="rerank-2.5",
            tokens=250,
            cost_usd=0.0000125,
            trace_id=trace_id,
            session_id=session_id,
            purpose="reranker",
            latency_ms=42,
        )

    record_api_call.assert_awaited_once()
    kwargs = record_api_call.await_args.kwargs
    assert kwargs["provider"] == "voyage"
    assert kwargs["model"] == "rerank-2.5"
    assert kwargs["input_tokens"] == 250
    assert kwargs["cost_usd"] == 0.0000125
    assert str(kwargs["trace_id"]) == trace_id
    assert str(kwargs["session_id"]) == session_id
    assert kwargs["purpose"] == "reranker"
    assert kwargs["latency_ms"] == 42
    instance.connect.assert_awaited_once()
    instance.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_records_with_system_session_sentinel() -> None:
    """SYSTEM_SESSION_ID is a valid UUID and must be threaded through, not skipped."""
    instance, record_api_call = _mock_cost_tracker_service()
    trace_id = str(uuid4())

    with patch("personal_agent.llm_client.cost_tracker.CostTrackerService", return_value=instance):
        await record_vendor_cost(
            provider="ovh",
            model="Qwen3-Embedding-8B",
            tokens=40,
            cost_usd=0.0000000456,
            trace_id=trace_id,
            session_id=SYSTEM_SESSION_ID,
            purpose="embedding",
        )

    record_api_call.assert_awaited_once()
    assert str(record_api_call.await_args.kwargs["session_id"]) == SYSTEM_SESSION_ID


@pytest.mark.asyncio
async def test_skips_on_missing_trace_id() -> None:
    """No trace_id -> never touches the DB, never raises."""
    instance, record_api_call = _mock_cost_tracker_service()

    with patch("personal_agent.llm_client.cost_tracker.CostTrackerService", return_value=instance):
        await record_vendor_cost(
            provider="ovh",
            model="Qwen3-Embedding-8B",
            tokens=10,
            cost_usd=0.00000001,
            trace_id=None,
            session_id=str(uuid4()),
            purpose="embedding",
        )

    record_api_call.assert_not_awaited()
    instance.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_on_missing_session_id() -> None:
    """No session_id -> never touches the DB, never raises."""
    instance, record_api_call = _mock_cost_tracker_service()

    with patch("personal_agent.llm_client.cost_tracker.CostTrackerService", return_value=instance):
        await record_vendor_cost(
            provider="ovh",
            model="Qwen3-Embedding-8B",
            tokens=10,
            cost_usd=0.00000001,
            trace_id=str(uuid4()),
            session_id=None,
            purpose="embedding",
        )

    record_api_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_on_malformed_identity() -> None:
    """A non-UUID string (e.g. the 'unknown' sentinel some tool call sites use) is skipped, not raised."""
    instance, record_api_call = _mock_cost_tracker_service()

    with patch("personal_agent.llm_client.cost_tracker.CostTrackerService", return_value=instance):
        await record_vendor_cost(
            provider="ovh",
            model="Qwen3-Embedding-8B",
            tokens=10,
            cost_usd=0.00000001,
            trace_id="unknown",
            session_id="unknown",
            purpose="embedding",
        )

    record_api_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_swallows_db_error_without_raising() -> None:
    """A DB failure inside record_api_call must not propagate out of this helper."""
    instance, record_api_call = _mock_cost_tracker_service()
    record_api_call.side_effect = RuntimeError("db exploded")

    with patch("personal_agent.llm_client.cost_tracker.CostTrackerService", return_value=instance):
        await record_vendor_cost(
            provider="voyage",
            model="rerank-2.5",
            tokens=100,
            cost_usd=0.000005,
            trace_id=str(uuid4()),
            session_id=str(uuid4()),
            purpose="reranker",
        )

    record_api_call.assert_awaited_once()


def test_sub_micro_dollar_cost_survives_decimal18_12_precision() -> None:
    """FRE-974: a single-token OVH call (~$0.000000114) must not round to zero.

    DECIMAL(10,6) (the pre-migration column type) rounds anything below
    $0.000001 to zero -- exactly the failure mode the ticket's "non-zero
    cost_usd" proof criterion would hit on a legitimately tiny call. The
    0022 migration widens api_costs.cost_usd to DECIMAL(18,12); this asserts
    the same Decimal(str(...)) conversion record_api_call performs
    (cost_tracker.py) preserves a sub-$0.000001 value at that precision,
    where it would have been lost at the old DECIMAL(10,6) scale.
    """
    cost_usd = 1 * 1e-7 * 1.14  # 1 OVH token, EUR->USD converted
    old_scale = Decimal(str(cost_usd)).quantize(Decimal("0.000001"))
    new_scale = Decimal(str(cost_usd)).quantize(Decimal("0.000000000001"))

    assert old_scale == Decimal("0.000000")  # the bug this migration fixes
    assert new_scale > 0  # DECIMAL(18,12) keeps it non-zero
