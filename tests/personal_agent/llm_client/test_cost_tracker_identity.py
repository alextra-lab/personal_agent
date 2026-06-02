"""ADR-0074 / FRE-376 — CostTracker.record_api_call identity contract.

These tests pin the write-time enforcement: the tracker must raise on a
missing trace_id or session_id, and the INSERT must carry both columns on
the happy path.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from personal_agent.exceptions import MissingIdentityError
from personal_agent.llm_client.cost_tracker import CostTrackerService


def _tracker_with_mock_pool() -> tuple[CostTrackerService, MagicMock, AsyncMock]:
    """Return a tracker wired to a mock pool + a mock acquire/fetchval chain.

    The async context manager around ``pool.acquire()`` is reproduced via
    ``MagicMock`` because asyncpg's real protocol is annoying to satisfy
    with plain ``AsyncMock``.
    """
    tracker = CostTrackerService()
    fetchval = AsyncMock(return_value=42)
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
async def test_record_api_call_raises_on_missing_trace_id() -> None:
    """A None trace_id must raise MissingIdentityError before touching the DB."""
    tracker, _conn, fetchval = _tracker_with_mock_pool()

    with pytest.raises(MissingIdentityError):
        await tracker.record_api_call(
            provider="anthropic",
            model="claude-sonnet-4.6",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.001,
            trace_id=None,  # type: ignore[arg-type]
            session_id=uuid4(),
        )
    fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_api_call_raises_on_missing_session_id() -> None:
    """A None session_id must raise MissingIdentityError before touching the DB."""
    tracker, _conn, fetchval = _tracker_with_mock_pool()

    with pytest.raises(MissingIdentityError):
        await tracker.record_api_call(
            provider="anthropic",
            model="claude-sonnet-4.6",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.001,
            trace_id=uuid4(),
            session_id=None,  # type: ignore[arg-type]
        )
    fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_api_call_threads_session_and_trace_into_insert() -> None:
    """Happy path: both identity columns land in the INSERT call args."""
    tracker, _conn, fetchval = _tracker_with_mock_pool()
    trace_id = uuid4()
    session_id = uuid4()

    record_id = await tracker.record_api_call(
        provider="openai",
        model="gpt-5.4-mini",
        input_tokens=100,
        output_tokens=20,
        cost_usd=0.0042,
        trace_id=trace_id,
        session_id=session_id,
        purpose="user_request",
        latency_ms=350,
    )

    assert record_id == 42
    fetchval.assert_awaited_once()
    args = fetchval.await_args.args
    # args[0] = SQL, args[1..] = parameters in INSERT column order:
    # timestamp, provider, model, input_tokens, output_tokens, cost_usd,
    # cache_read_input_tokens, cache_creation_input_tokens,
    # trace_id, session_id, purpose, latency_ms
    sql = args[0]
    assert "session_id" in sql
    assert args[2] == "openai"
    assert args[3] == "gpt-5.4-mini"
    assert args[4] == 100
    assert args[5] == 20
    assert args[6] == Decimal("0.0042")
    assert args[7] is None  # cache_read_input_tokens (not an Anthropic call)
    assert args[8] is None  # cache_creation_input_tokens
    assert args[9] == trace_id
    assert args[10] == session_id
    assert args[11] == "user_request"
    assert args[12] == 350
