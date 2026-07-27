"""FRE-988 — cost tracker holds one pooled connection for its process lifetime.

Prior to this ticket, every priced call (LiteLLM chat, vendor embedding/rerank)
built its own ``CostTrackerService()``, opened a fresh asyncpg pool, and tore
it down again — 527 connect/disconnect pairs/day tracking the call count
instead of a stable pool. These tests pin the fix: ``connect()`` is idempotent
and the module exposes one process-wide singleton every call site shares.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.llm_client.cost_tracker import (
    CostTrackerService,
    cost_tracker_service,
    get_cost_tracker_service,
)


def test_get_cost_tracker_service_returns_same_instance_across_calls() -> None:
    """Every call site must share one tracker, not build its own."""
    first = get_cost_tracker_service()
    second = get_cost_tracker_service()

    assert first is second
    assert first is cost_tracker_service


@pytest.mark.asyncio
async def test_connect_is_idempotent() -> None:
    """A second connect() while already connected must not open a new pool.

    Mirrors ``RouteTraceLedger.connect``'s idempotency guard — the shared
    singleton is connected once (e.g. at app startup) and every subsequent
    call-site ``await tracker.connect()`` before use must be a cheap no-op.
    """
    tracker = CostTrackerService()
    sentinel_pool = AsyncMock()

    with patch(
        "personal_agent.llm_client.cost_tracker.asyncpg.create_pool",
        AsyncMock(return_value=sentinel_pool),
    ) as mock_create_pool:
        await tracker.connect()
        await tracker.connect()
        await tracker.connect()

        mock_create_pool.assert_awaited_once()

    assert tracker.pool is sentinel_pool


@pytest.mark.asyncio
async def test_connect_after_disconnect_reconnects() -> None:
    """disconnect() clears the pool, so a subsequent connect() must reconnect."""
    tracker = CostTrackerService()
    first_pool = AsyncMock()
    second_pool = AsyncMock()

    with patch(
        "personal_agent.llm_client.cost_tracker.asyncpg.create_pool",
        AsyncMock(side_effect=[first_pool, second_pool]),
    ) as mock_create_pool:
        await tracker.connect()
        assert tracker.pool is first_pool

        await tracker.disconnect()
        assert tracker.pool is None

        await tracker.connect()
        assert tracker.pool is second_pool

        assert mock_create_pool.await_count == 2
