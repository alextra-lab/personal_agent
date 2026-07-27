"""FRE-988 — cost tracker holds one pooled connection for its process lifetime.

Prior to this ticket, every priced call (LiteLLM chat, vendor embedding/rerank)
built its own ``CostTrackerService()``, opened a fresh asyncpg pool, and tore
it down again — 527 connect/disconnect pairs/day tracking the call count
instead of a stable pool. These tests pin the fix: ``connect()`` is idempotent
while the pool is live, rebuilds it if the pool object itself has gone
terminal, and the module exposes one process-wide singleton every call site
shares.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.llm_client.cost_tracker import (
    CostTrackerService,
    cost_tracker_service,
    get_cost_tracker_service,
)


def _mock_pool(*, is_closing: bool = False) -> MagicMock:
    """Build a mock asyncpg.Pool: ``is_closing()`` is sync, ``close()`` is async."""
    pool = MagicMock()
    pool.is_closing = MagicMock(return_value=is_closing)
    pool.close = AsyncMock()
    return pool


def test_get_cost_tracker_service_returns_same_instance_across_calls() -> None:
    """Every call site must share one tracker, not build its own."""
    first = get_cost_tracker_service()
    second = get_cost_tracker_service()

    assert first is second
    assert first is cost_tracker_service


@pytest.mark.asyncio
async def test_connect_is_idempotent_while_pool_is_live() -> None:
    """A second connect() while the pool is live must not open a new pool.

    Mirrors ``RouteTraceLedger.connect``'s idempotency guard — the shared
    singleton is connected once (e.g. at app startup) and every subsequent
    call-site ``await tracker.connect()`` before use must be a cheap no-op.
    """
    tracker = CostTrackerService()
    sentinel_pool = _mock_pool(is_closing=False)

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
    first_pool = _mock_pool()
    second_pool = _mock_pool()

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


@pytest.mark.asyncio
async def test_connect_rebuilds_a_pool_that_has_gone_terminal() -> None:
    """A pool object that reports itself closed/closing must be rebuilt, not
    left as a permanent no-op (the gap flagged at the master gate: a per-call
    pool self-healed by construction every call; the shared singleton must
    replicate that self-healing for the narrow case where the pool itself —
    not just one connection inside it — has gone terminal).
    """
    tracker = CostTrackerService()
    dead_pool = _mock_pool(is_closing=True)
    fresh_pool = _mock_pool(is_closing=False)

    with patch(
        "personal_agent.llm_client.cost_tracker.asyncpg.create_pool",
        AsyncMock(side_effect=[dead_pool, fresh_pool]),
    ) as mock_create_pool:
        await tracker.connect()
        assert tracker.pool is dead_pool

        # Something outside this class closed/terminated the pool (or it
        # went terminal on its own) — is_closing() now reports True.
        await tracker.connect()

        assert mock_create_pool.await_count == 2
        assert tracker.pool is fresh_pool


@pytest.mark.asyncio
async def test_concurrent_initial_connects_build_exactly_one_pool() -> None:
    """N coroutines racing a still-empty pool must produce exactly one pool.

    Flagged by codex plan-review (FRE-988 re-review): a bare check-then-act on
    ``self.pool`` lets concurrent first-connects race, each seeing pool=None
    and building its own pool. The singleton is reachable from concurrent
    priced calls (and from standalone scripts with no FastAPI lifespan
    serializing a single startup connect), so this must hold without help
    from an external caller.
    """
    tracker = CostTrackerService()
    sentinel_pool = _mock_pool()

    async def _slow_create_pool(*args: object, **kwargs: object) -> MagicMock:
        # Yield control so concurrently-scheduled connect() calls actually
        # interleave instead of trivially running one at a time regardless
        # of locking (which would make the test pass even without a fix).
        await asyncio.sleep(0.01)
        return sentinel_pool

    with patch(
        "personal_agent.llm_client.cost_tracker.asyncpg.create_pool",
        AsyncMock(side_effect=_slow_create_pool),
    ) as mock_create_pool:
        await asyncio.gather(*(tracker.connect() for _ in range(8)))

        mock_create_pool.assert_awaited_once()

    assert tracker.pool is sentinel_pool


@pytest.mark.asyncio
async def test_a_failed_connect_does_not_corrupt_a_later_successful_one() -> None:
    """A failed connect() attempt must never clear a pool it didn't build.

    The race codex flagged: two concurrent attempts, one fails and its
    except-branch resets ``self.pool = None`` — if that runs *after* a
    sibling attempt already installed a good pool, the good pool would be
    silently discarded. The lock makes concurrent attempts fully serialized
    (one attempt's failure or success always completes before the next
    attempt's own check-and-build begins), so a sequential fail-then-succeed
    is the correct proxy for the concurrent case: exercised concurrently via
    ``test_concurrent_initial_connects_build_exactly_one_pool`` above, this
    test pins the specific failure-handling half of that guarantee.
    """
    tracker = CostTrackerService()

    with patch(
        "personal_agent.llm_client.cost_tracker.asyncpg.create_pool",
        AsyncMock(side_effect=RuntimeError("connection refused")),
    ):
        await tracker.connect()

    assert tracker.pool is None

    sentinel_pool = _mock_pool()
    with patch(
        "personal_agent.llm_client.cost_tracker.asyncpg.create_pool",
        AsyncMock(return_value=sentinel_pool),
    ) as mock_create_pool:
        await tracker.connect()

        mock_create_pool.assert_awaited_once()

    assert tracker.pool is sentinel_pool


@pytest.mark.asyncio
async def test_concurrent_recovery_from_a_terminal_pool_builds_one_pool() -> None:
    """N coroutines racing a rebuild of a terminal pool must agree on one pool."""
    tracker = CostTrackerService()
    tracker.pool = _mock_pool(is_closing=True)
    fresh_pool = _mock_pool(is_closing=False)

    async def _slow_create_pool(*args: object, **kwargs: object) -> MagicMock:
        await asyncio.sleep(0.01)
        return fresh_pool

    with patch(
        "personal_agent.llm_client.cost_tracker.asyncpg.create_pool",
        AsyncMock(side_effect=_slow_create_pool),
    ) as mock_create_pool:
        await asyncio.gather(*(tracker.connect() for _ in range(8)))

        mock_create_pool.assert_awaited_once()

    assert tracker.pool is fresh_pool
