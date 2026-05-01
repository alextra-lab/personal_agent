"""Concurrent reservation contention test (FRE-304 acceptance criterion).

The whole point of the gate is that ``SELECT … FOR UPDATE`` serialises
concurrent reservations. With N=50 async tasks racing against a cap that
admits exactly K calls, the gate must approve exactly K and deny N−K, and
the final counter must equal K × amount (no double-counting, no lost
reservations).

This test reproduces the original 2026-04-30 incident shape — multiple
consumer-group workers hitting cloud LLMs in parallel — except now the
gate prevents the overshoot.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import asyncpg
import pytest

from personal_agent.config import settings
from personal_agent.cost_gate import BudgetDenied, CostGate
from personal_agent.cost_gate.gate import _window_start

from .conftest import _build_config

pytestmark = pytest.mark.integration

# 50 racers, each reserving $0.20, against a $1.00 cap → exactly 5 wins.
NUM_RACERS = 50
PER_RESERVATION = Decimal("0.20")
DAILY_CAP = Decimal("1.00")
EXPECTED_WINS = int(DAILY_CAP / PER_RESERVATION)  # 5


@pytest.mark.asyncio
async def test_concurrent_reservations_serialise(unique_role: str) -> None:
    """50 racers, $0.20 each, $1.00 cap: exactly 5 succeed, 45 raise BudgetDenied."""
    config = _build_config(
        unique_role,
        daily_cap=DAILY_CAP,
        weekly_total_cap=Decimal("100.00"),  # generous so only daily denies
    )
    gate = CostGate(config=config, db_url=settings.database_url)
    await gate.connect()
    try:

        async def attempt() -> bool:
            try:
                await gate.reserve(unique_role, PER_RESERVATION)
                return True
            except BudgetDenied:
                return False

        results = await asyncio.gather(*[attempt() for _ in range(NUM_RACERS)])

        wins = sum(results)
        losses = NUM_RACERS - wins
        assert wins == EXPECTED_WINS, (
            f"expected exactly {EXPECTED_WINS} wins, got {wins} (losses: {losses})"
        )

        # Final counter equals the sum of all winning reservations.
        pool = await asyncpg.create_pool(gate.db_url, min_size=1, max_size=2)
        assert pool is not None
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT running_total FROM budget_counters
                     WHERE user_id IS NULL AND time_window = 'daily'
                       AND provider IS NULL AND role = $1
                       AND window_start = $2
                    """,
                    unique_role,
                    _window_start("daily"),
                )
            assert row is not None
            assert Decimal(row["running_total"]) == DAILY_CAP, (
                f"counter should equal cap exactly; got {row['running_total']}"
            )
        finally:
            await pool.close()
    finally:
        await gate.disconnect()
