"""Test fixtures for the Cost Check Gate (ADR-0065 / FRE-304).

Each test gets a connected ``CostGate`` against the running cloud-sim-postgres
plus a unique role identifier so concurrent test runs don't share counter
rows. Tests build their own ``BudgetConfig`` inline so they can pin tight
caps for denial / contention scenarios.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.config import settings
from personal_agent.cost_gate import (
    BudgetConfig,
    CapEntry,
    CostGate,
    OnDenialBehaviour,
    RoleConfig,
)
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn


def _build_config(role: str, *, daily_cap: Decimal, weekly_total_cap: Decimal) -> BudgetConfig:
    """Build a minimal config for one role + a _total weekly cap.

    Tests pin both a per-role daily cap and a global weekly _total so they
    exercise the multi-cap locking logic the same way prod does.
    """
    return BudgetConfig(
        version=1,
        roles={
            role: RoleConfig(
                default_output_tokens=256,
                safety_factor=1.2,
                on_denial=OnDenialBehaviour.RAISE,
            ),
        },
        caps=[
            CapEntry(time_window="daily", role=role, cap_usd=daily_cap),
            CapEntry(time_window="weekly", role="_total", cap_usd=weekly_total_cap),
        ],
    )


@pytest.fixture
def unique_role() -> str:
    """Return a role name unique to this test invocation.

    Using a UUID-derived suffix keeps test runs from colliding on the same
    counter rows when run in parallel or against a long-lived dev DB.
    """
    return f"test_{uuid4().hex[:8]}"


@pytest.fixture
def budget_config(unique_role: str) -> BudgetConfig:
    """Default per-test config — generous caps, fits the happy-path tests.

    Override per-test with a fresh ``_build_config(role, ...)`` when tighter
    caps are needed (denial / contention).
    """
    return _build_config(
        unique_role, daily_cap=Decimal("10.00"), weekly_total_cap=Decimal("100.00")
    )


@pytest_asyncio.fixture
async def cost_gate(budget_config: BudgetConfig) -> AsyncIterator[CostGate]:
    """A connected ``CostGate`` against the running Postgres."""
    gate = CostGate(config=budget_config, db_url=settings.database_url)
    await gate.connect()
    try:
        yield gate
    finally:
        await gate.disconnect()


@pytest_asyncio.fixture
async def db_pool() -> AsyncIterator[asyncpg.Pool]:
    """Direct asyncpg pool for tests that need to inspect counter rows."""
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.database_url),
        min_size=1,
        max_size=2,
        command_timeout=10,
    )
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_test_rows(unique_role: str) -> AsyncIterator[None]:
    """Drop counter / reservation pollution and revert ``_total`` weekly.

    Captures the ``_total`` weekly ``running_total`` before the test, and
    after the test:

    1. Deletes every reservation referencing the test's role.
    2. Deletes every counter row for the test's role.
    3. Restores the ``_total`` weekly counter to its pre-test value, since
       any winning reservation against ``_total`` would otherwise leak into
       the prod-visible counter.

    Without this fixture the dev DB's ``_total`` weekly steadily grows with
    every test run, eventually overlapping the real prod cap.
    """
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.database_url),
        min_size=1,
        max_size=1,
        command_timeout=10,
    )
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT running_total FROM budget_counters
                 WHERE user_id IS NULL AND time_window = 'weekly'
                   AND provider IS NULL AND role = '_total'
                """
            )
            pre_total_weekly: Decimal | None = row["running_total"] if row else None

        yield

        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM budget_reservations WHERE role = $1",
                unique_role,
            )
            await conn.execute(
                "DELETE FROM budget_counters WHERE role = $1",
                unique_role,
            )
            if pre_total_weekly is not None:
                await conn.execute(
                    """
                    UPDATE budget_counters
                       SET running_total = $1, updated_at = NOW()
                     WHERE user_id IS NULL AND time_window = 'weekly'
                       AND provider IS NULL AND role = '_total'
                    """,
                    pre_total_weekly,
                )
    finally:
        await pool.close()


__all__ = ["_build_config"]
