"""Cost Gate happy-path / denial / refund / idempotency tests (FRE-304).

These tests hit the running Postgres (``cloud-sim-postgres``) so the
``SELECT … FOR UPDATE`` locking is exercised end-to-end. Each test uses a
unique role so concurrent runs and the ``_total`` row don't interfere.

Marker: ``integration`` — excluded from ``make test`` because they need a
live DB. Run with ``pytest tests/personal_agent/cost_gate/`` directly.
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg
import pytest

from personal_agent.cost_gate import BudgetDenied, CostGate, DenialReason
from personal_agent.cost_gate.gate import _window_start

from .conftest import _build_config

pytestmark = pytest.mark.integration


async def _counter_total(pool: asyncpg.Pool, role: str, time_window: str) -> Decimal:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT running_total FROM budget_counters
             WHERE user_id IS NULL AND time_window = $1
               AND provider IS NULL AND role = $2
               AND window_start = $3
            """,
            time_window,
            role,
            _window_start(time_window),
        )
        return Decimal(row["running_total"]) if row else Decimal("0")


@pytest.mark.asyncio
async def test_reserve_then_commit_settles_actual(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str
) -> None:
    """Estimate $1.00, actual $0.40 → commit refunds the $0.60 difference."""
    rid = await cost_gate.reserve(unique_role, Decimal("1.00"))
    after_reserve = await _counter_total(db_pool, unique_role, "daily")
    assert after_reserve == Decimal("1.000000")

    await cost_gate.commit(rid, Decimal("0.40"))
    after_commit = await _counter_total(db_pool, unique_role, "daily")
    assert after_commit == Decimal("0.400000"), "commit should settle to actual cost"


@pytest.mark.asyncio
async def test_reserve_then_refund_returns_to_zero(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str
) -> None:
    """Refund decrements the counter back to its pre-reservation value."""
    before = await _counter_total(db_pool, unique_role, "daily")
    rid = await cost_gate.reserve(unique_role, Decimal("0.75"))
    await cost_gate.refund(rid)
    after = await _counter_total(db_pool, unique_role, "daily")
    assert after == before


@pytest.mark.asyncio
async def test_reserve_denied_when_cap_exceeded(unique_role: str) -> None:
    """A reservation larger than the cap raises BudgetDenied with payload."""
    config = _build_config(
        unique_role, daily_cap=Decimal("0.50"), weekly_total_cap=Decimal("100.00")
    )
    from personal_agent.config import settings

    gate = CostGate(config=config, db_url=settings.database_url)
    await gate.connect()
    try:
        with pytest.raises(BudgetDenied) as exc_info:
            await gate.reserve(unique_role, Decimal("0.75"))
        denial = exc_info.value
        assert denial.role == unique_role
        assert denial.time_window == "daily"
        assert denial.cap == Decimal("0.500000")
        assert denial.denial_reason == DenialReason.CAP_EXCEEDED.value
    finally:
        await gate.disconnect()


@pytest.mark.asyncio
async def test_denial_does_not_increment_counter(db_pool: asyncpg.Pool, unique_role: str) -> None:
    """A denied reservation rolls back; running_total is untouched."""
    config = _build_config(
        unique_role, daily_cap=Decimal("0.50"), weekly_total_cap=Decimal("100.00")
    )
    from personal_agent.config import settings

    gate = CostGate(config=config, db_url=settings.database_url)
    await gate.connect()
    try:
        before = await _counter_total(db_pool, unique_role, "daily")
        with pytest.raises(BudgetDenied):
            await gate.reserve(unique_role, Decimal("99.99"))
        after = await _counter_total(db_pool, unique_role, "daily")
        assert after == before
    finally:
        await gate.disconnect()


@pytest.mark.asyncio
async def test_refund_is_idempotent(cost_gate: CostGate, unique_role: str) -> None:
    """A second refund of the same reservation is a logged no-op."""
    rid = await cost_gate.reserve(unique_role, Decimal("0.30"))
    await cost_gate.refund(rid)
    await cost_gate.refund(rid)  # must not raise


@pytest.mark.asyncio
async def test_cannot_refund_committed_reservation(cost_gate: CostGate, unique_role: str) -> None:
    """Refunding an already-committed reservation is an error — actual cost is on the books."""
    rid = await cost_gate.reserve(unique_role, Decimal("0.30"))
    await cost_gate.commit(rid, Decimal("0.20"))
    with pytest.raises(ValueError, match="already committed"):
        await cost_gate.refund(rid)


@pytest.mark.asyncio
async def test_total_cap_blocks_role_below_its_own_cap(unique_role: str) -> None:
    """Even when the per-role cap is roomy, the _total cap can deny.

    Verifies the synthetic _total row is locked alongside the per-role row
    and the most-restrictive cap wins.
    """
    config = _build_config(
        unique_role, daily_cap=Decimal("100.00"), weekly_total_cap=Decimal("0.10")
    )
    from personal_agent.config import settings

    gate = CostGate(config=config, db_url=settings.database_url)
    await gate.connect()
    try:
        with pytest.raises(BudgetDenied) as exc_info:
            await gate.reserve(unique_role, Decimal("1.00"))
        # The _total row should be the one that denies, not the per-role daily.
        assert exc_info.value.role == "_total"
        assert exc_info.value.time_window == "weekly"
    finally:
        await gate.disconnect()
