"""Tests for ``cost_gate.policy`` — the ``budget_policies`` sync (FRE-1209).

``budget_policies`` had no live write path before ``sync_budget_policies_to_db``
existed (verified by grep and a live row count of 0) — nothing wrote to it,
which defeated any dashboard panel joining against it. These tests exercise
the sync directly against the isolated test Postgres (FRE-375 — ``db_pool``
resolves to :5433 under ``APP_ENV=test``, never the production substrate).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.cost_gate import BudgetConfig, CapEntry, OnDenialBehaviour, RoleConfig
from personal_agent.cost_gate.policy import sync_budget_policies_to_db

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def _restore_v1_policies(db_pool: asyncpg.Pool) -> AsyncIterator[None]:
    """Snapshot and restore the v1-scoped rows the sync full-replaces.

    ``sync_budget_policies_to_db`` deletes-then-inserts the whole
    ``user_id IS NULL AND provider IS NULL`` scope, so a test run must not
    leak into, or be polluted by, whatever the real service last synced.
    """
    async with db_pool.acquire() as conn:
        pre_rows = await conn.fetch(
            "SELECT time_window, role, cap_usd FROM budget_policies "
            "WHERE user_id IS NULL AND provider IS NULL"
        )
    yield
    async with db_pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM budget_policies WHERE user_id IS NULL AND provider IS NULL")
        await conn.executemany(
            "INSERT INTO budget_policies (time_window, role, cap_usd) VALUES ($1, $2, $3)",
            [(r["time_window"], r["role"], r["cap_usd"]) for r in pre_rows],
        )


def _config(caps: list[CapEntry]) -> BudgetConfig:
    return BudgetConfig(
        version=1,
        roles={
            "main_inference": RoleConfig(
                default_output_tokens=256, safety_factor=1.2, on_denial=OnDenialBehaviour.RAISE
            ),
        },
        caps=caps,
    )


@pytest.mark.asyncio
async def test_sync_writes_real_rows_matching_yaml(db_pool: asyncpg.Pool) -> None:
    """A cap declared in config lands as a real, matching row in the table."""
    role = f"test_{uuid4().hex[:8]}"
    config = _config([CapEntry(time_window="daily", role=role, cap_usd=Decimal("12.50"))])

    await sync_budget_policies_to_db(config, db_pool)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT cap_usd FROM budget_policies WHERE role = $1 AND time_window = 'daily'",
            role,
        )
    assert row is not None
    assert row["cap_usd"] == Decimal("12.50")


@pytest.mark.asyncio
async def test_sync_removes_stale_v1_row_no_longer_in_yaml(db_pool: asyncpg.Pool) -> None:
    """A role dropped from config must not linger as a stale DB row."""
    stale_role = f"test_{uuid4().hex[:8]}"
    kept_role = f"test_{uuid4().hex[:8]}"

    await sync_budget_policies_to_db(
        _config([CapEntry(time_window="daily", role=stale_role, cap_usd=Decimal("5.00"))]),
        db_pool,
    )
    # Second sync's config no longer names stale_role — it must not linger.
    await sync_budget_policies_to_db(
        _config([CapEntry(time_window="daily", role=kept_role, cap_usd=Decimal("7.00"))]),
        db_pool,
    )

    async with db_pool.acquire() as conn:
        stale = await conn.fetchrow("SELECT 1 FROM budget_policies WHERE role = $1", stale_role)
        kept = await conn.fetchrow("SELECT cap_usd FROM budget_policies WHERE role = $1", kept_role)
    assert stale is None
    assert kept is not None
    assert kept["cap_usd"] == Decimal("7.00")


@pytest.mark.asyncio
async def test_sync_leaves_v2_scoped_rows_untouched(db_pool: asyncpg.Pool) -> None:
    """A future per-user/per-provider row must survive a v1 sync untouched."""
    v2_role = f"test_{uuid4().hex[:8]}"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO budget_policies (user_id, time_window, provider, role, cap_usd)
            VALUES (gen_random_uuid(), 'daily', 'anthropic', $1, 3.00)
            """,
            v2_role,
        )
    try:
        await sync_budget_policies_to_db(_config([]), db_pool)

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT cap_usd FROM budget_policies WHERE role = $1", v2_role
            )
        assert row is not None
        assert row["cap_usd"] == Decimal("3.00")
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM budget_policies WHERE role = $1", v2_role)
