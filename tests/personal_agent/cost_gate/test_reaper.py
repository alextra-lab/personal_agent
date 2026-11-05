"""Reaper TTL reclamation tests (FRE-304 acceptance criterion).

A reservation older than its TTL must be reclaimed by ``reap_stale``: the
row transitions ``active -> expired`` and the counter decrements by the
reservation amount. This catches caller crashes between ``reserve()`` and
``commit()``/``refund()``.

We force-age the reservation by direct SQL update rather than waiting 90
seconds — the reaper logic only checks ``expires_at < NOW()``.
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg
import pytest

from personal_agent.config import settings
from personal_agent.cost_gate import (
    BudgetConfig,
    CapEntry,
    CostGate,
    OnDenialBehaviour,
    ReservationStatus,
    RoleConfig,
)
from personal_agent.cost_gate.gate import _window_start

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_reaper_sweeps_stale_active_reservation(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str
) -> None:
    """An active reservation past its TTL becomes expired + counter is refunded."""
    rid = await cost_gate.reserve(unique_role, Decimal("0.40"))

    async with db_pool.acquire() as conn:
        # Force the reservation past its TTL.
        await conn.execute(
            "UPDATE budget_reservations SET expires_at = NOW() - interval '1 second' WHERE reservation_id = $1",
            rid,
        )
        before = await conn.fetchval(
            """
            SELECT running_total FROM budget_counters
             WHERE user_id IS NULL AND time_window = 'daily'
               AND provider IS NULL AND role = $1
               AND window_start = $2
            """,
            unique_role,
            _window_start("daily"),
        )
        assert Decimal(before) == Decimal("0.400000")

    swept = await cost_gate.reap_stale()
    assert swept >= 1, "reaper should sweep at least the one stale reservation"

    async with db_pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM budget_reservations WHERE reservation_id = $1", rid
        )
        assert status == ReservationStatus.EXPIRED.value

        after = await conn.fetchval(
            """
            SELECT running_total FROM budget_counters
             WHERE user_id IS NULL AND time_window = 'daily'
               AND provider IS NULL AND role = $1
               AND window_start = $2
            """,
            unique_role,
            _window_start("daily"),
        )
        assert Decimal(after) == Decimal("0"), "counter should be refunded by the reaper"


@pytest.mark.asyncio
async def test_reaper_noop_when_no_stale_rows(cost_gate: CostGate) -> None:
    """A sweep with no stale rows returns 0 and doesn't error."""
    swept = await cost_gate.reap_stale()
    assert swept >= 0  # only counts rows it actually expired, may be 0


@pytest.mark.asyncio
async def test_reaper_batch_that_shares_a_counter_refunds_each_reservation(
    db_pool: asyncpg.Pool, unique_role: str
) -> None:
    """Two expired reservations share ``_total``, and each one's amount comes off it (FRE-1733)."""
    other_role = f"{unique_role}_b"
    estimate = RoleConfig(
        default_output_tokens=256, safety_factor=1.2, on_denial=OnDenialBehaviour.RAISE
    )
    config = BudgetConfig(
        version=1,
        roles={unique_role: estimate, other_role: estimate},
        caps=[
            CapEntry(time_window="daily", role=unique_role, cap_usd=Decimal("10.00")),
            CapEntry(time_window="daily", role=other_role, cap_usd=Decimal("10.00")),
            CapEntry(time_window="weekly", role="_total", cap_usd=Decimal("100.00")),
        ],
    )
    gate = CostGate(config=config, db_url=settings.database_url)
    await gate.connect()

    async def _counter(role: str, window: str) -> Decimal:
        async with db_pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT running_total FROM budget_counters
                 WHERE user_id IS NULL AND time_window = $1
                   AND provider IS NULL AND role = $2
                   AND window_start = $3
                """,
                window,
                role,
                _window_start(window),
            )
        return Decimal(value) if value is not None else Decimal(0)

    try:
        await gate.reap_stale()  # no stale row of another test changes the totals below
        total_before = await _counter("_total", "weekly")
        first = await gate.reserve(unique_role, Decimal("0.40"))
        second = await gate.reserve(other_role, Decimal("0.25"))
        assert await _counter("_total", "weekly") == total_before + Decimal("0.65")
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE budget_reservations SET expires_at = NOW() - interval '1 second' "
                "WHERE reservation_id = ANY($1::uuid[])",
                [first, second],
            )

        assert await gate.reap_stale() == 2

        assert await _counter("_total", "weekly") == total_before
        assert await _counter(unique_role, "daily") == Decimal(0)
        assert await _counter(other_role, "daily") == Decimal(0)
    finally:
        await gate.disconnect()
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM budget_reservations WHERE role = $1", other_role)
            await conn.execute("DELETE FROM budget_counters WHERE role = $1", other_role)
