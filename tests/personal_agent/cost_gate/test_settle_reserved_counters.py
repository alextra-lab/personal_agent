"""A reservation settles against the counters it reserved (FRE-1681, FRE-1690).

FRE-1681: a call that outlives ``RESERVATION_TTL_SECONDS`` is expired by the reaper.
Its real cost must still reach ``budget_counters`` when the call ends. A call that
never ends must still be freed by the reaper.

FRE-1690: ``commit`` / ``refund`` / the reaper adjust the counters that ``reserve``
incremented, not the counters of the window that is current at settle time.

These tests hit the running test Postgres. A fake clock moves the window that
``reserve`` sees. The config has per-role caps only, so no shared ``_total`` row is
written under a fake date.

Marker: ``integration`` — excluded from ``make test`` because they need a live DB.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg
import pytest

from personal_agent.cost_gate import (
    BudgetConfig,
    CapEntry,
    CostGate,
    OnDenialBehaviour,
    ReservationStatus,
    RoleConfig,
)
from personal_agent.cost_gate import gate as gate_module
from personal_agent.cost_gate.gate import _window_start

pytestmark = pytest.mark.integration

# 2026-10-24 is a Saturday: its midnight is a daily boundary only.
SAT_BEFORE_MIDNIGHT = datetime(2026, 10, 24, 23, 59, 30, tzinfo=timezone.utc)
SUN_AFTER_MIDNIGHT = datetime(2026, 10, 25, 0, 0, 30, tzinfo=timezone.utc)
# 2026-10-26 is a Monday: Sunday midnight is a daily and a weekly boundary.
SUN_BEFORE_MONDAY = datetime(2026, 10, 25, 23, 59, 30, tzinfo=timezone.utc)
MON_AFTER_MIDNIGHT = datetime(2026, 10, 26, 0, 0, 30, tzinfo=timezone.utc)


class _FakeClock:
    """Replacement for the ``datetime`` name inside ``cost_gate.gate``."""

    current: datetime = SAT_BEFORE_MIDNIGHT

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        return cls.current


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_FakeClock]]:
    """Freeze the gate's idea of "now"; tests move it with ``clock.current = ...``."""
    monkeypatch.setattr(gate_module, "datetime", _FakeClock)
    yield _FakeClock


@pytest.fixture
def budget_config(unique_role: str) -> BudgetConfig:
    """Per-role daily and weekly caps, no ``_total`` cap."""
    return BudgetConfig(
        version=1,
        roles={
            unique_role: RoleConfig(
                default_output_tokens=256,
                safety_factor=1.2,
                on_denial=OnDenialBehaviour.RAISE,
            ),
        },
        caps=[
            CapEntry(time_window="daily", role=unique_role, cap_usd=Decimal("10.00")),
            CapEntry(time_window="weekly", role=unique_role, cap_usd=Decimal("100.00")),
        ],
    )


async def _total(pool: asyncpg.Pool, role: str, time_window: str, at: datetime) -> Decimal | None:
    """Return the counter of the window that contains ``at``; ``None`` if no row exists."""
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            """
            SELECT running_total FROM budget_counters
             WHERE user_id IS NULL AND time_window = $1
               AND provider IS NULL AND role = $2
               AND window_start = $3
            """,
            time_window,
            role,
            _window_start(time_window, at),
        )
    return None if value is None else Decimal(value)


async def _assert_no_negative_counter(pool: asyncpg.Pool, role: str) -> None:
    async with pool.acquire() as conn:
        lowest = await conn.fetchval(
            "SELECT MIN(running_total) FROM budget_counters WHERE role = $1", role
        )
    assert lowest is None or Decimal(lowest) >= 0, f"a counter went negative: {lowest}"


async def _status(pool: asyncpg.Pool, reservation_id: object) -> str:
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT status FROM budget_reservations WHERE reservation_id = $1", reservation_id
        )
    return str(value)


async def _expire_and_reap(gate: CostGate, pool: asyncpg.Pool, reservation_id: object) -> None:
    """Force the reservation past its TTL, then run the reaper once."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE budget_reservations SET expires_at = NOW() - interval '1 second' "
            "WHERE reservation_id = $1",
            reservation_id,
        )
    await gate.reap_stale()


# ---------------------------------------------------------------------------
# FRE-1681 — a call longer than the reservation lifetime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_commit_charges_real_cost(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str
) -> None:
    """AC-1: the reaper expires the reservation, the call ends later, the cost is charged."""
    now = datetime.now(timezone.utc)
    rid = await cost_gate.reserve(unique_role, Decimal("0.40"))
    await _expire_and_reap(cost_gate, db_pool, rid)
    assert await _status(db_pool, rid) == ReservationStatus.EXPIRED.value
    assert await _total(db_pool, unique_role, "daily", now) == Decimal("0")

    await cost_gate.commit(rid, Decimal("0.34"))

    assert await _total(db_pool, unique_role, "daily", now) == Decimal("0.34")
    assert await _total(db_pool, unique_role, "weekly", now) == Decimal("0.34")
    assert await _status(db_pool, rid) == ReservationStatus.COMMITTED.value
    async with db_pool.acquire() as conn:
        actual = await conn.fetchval(
            "SELECT actual_cost_usd FROM budget_reservations WHERE reservation_id = $1", rid
        )
    assert Decimal(actual) == Decimal("0.34")


@pytest.mark.asyncio
async def test_late_commit_twice_raises_and_charges_once(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str
) -> None:
    """A second commit of a settled reservation raises and adds nothing."""
    now = datetime.now(timezone.utc)
    rid = await cost_gate.reserve(unique_role, Decimal("0.40"))
    await _expire_and_reap(cost_gate, db_pool, rid)
    await cost_gate.commit(rid, Decimal("0.34"))

    with pytest.raises(ValueError, match="committed"):
        await cost_gate.commit(rid, Decimal("0.34"))
    assert await _total(db_pool, unique_role, "daily", now) == Decimal("0.34")


@pytest.mark.asyncio
async def test_commit_inside_lifetime_is_unchanged(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str
) -> None:
    """AC-2: estimate 1.00, actual 0.40 settles to 0.40 on both counters, as before."""
    now = datetime.now(timezone.utc)
    rid = await cost_gate.reserve(unique_role, Decimal("1.00"))
    await cost_gate.commit(rid, Decimal("0.40"))

    assert await _total(db_pool, unique_role, "daily", now) == Decimal("0.40")
    assert await _total(db_pool, unique_role, "weekly", now) == Decimal("0.40")
    assert await _status(db_pool, rid) == ReservationStatus.COMMITTED.value


@pytest.mark.asyncio
async def test_reaper_still_frees_a_call_that_never_ends(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str
) -> None:
    """AC-3: no commit and no refund → the reaper frees the estimate, once."""
    now = datetime.now(timezone.utc)
    rid = await cost_gate.reserve(unique_role, Decimal("0.40"))
    assert await _total(db_pool, unique_role, "daily", now) == Decimal("0.40")

    await _expire_and_reap(cost_gate, db_pool, rid)
    assert await _status(db_pool, rid) == ReservationStatus.EXPIRED.value
    assert await _total(db_pool, unique_role, "daily", now) == Decimal("0")
    assert await _total(db_pool, unique_role, "weekly", now) == Decimal("0")

    await cost_gate.reap_stale()  # a second sweep must not free it again
    assert await _total(db_pool, unique_role, "daily", now) == Decimal("0")
    await _assert_no_negative_counter(db_pool, unique_role)


# ---------------------------------------------------------------------------
# FRE-1690 — settle against the reserved window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_after_daily_boundary_settles_reserved_window(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str, clock: type[_FakeClock]
) -> None:
    """Reserve 23:59:30, commit 00:00:30: the old day carries the actual cost."""
    clock.current = SAT_BEFORE_MIDNIGHT
    rid = await cost_gate.reserve(unique_role, Decimal("1.00"))

    clock.current = SUN_AFTER_MIDNIGHT
    await cost_gate.commit(rid, Decimal("0.40"))

    assert await _total(db_pool, unique_role, "daily", SAT_BEFORE_MIDNIGHT) == Decimal("0.40")
    assert await _total(db_pool, unique_role, "daily", SUN_AFTER_MIDNIGHT) is None
    # Same ISO week: the weekly counter is shared by both days.
    assert await _total(db_pool, unique_role, "weekly", SAT_BEFORE_MIDNIGHT) == Decimal("0.40")
    await _assert_no_negative_counter(db_pool, unique_role)


@pytest.mark.asyncio
async def test_commit_after_weekly_boundary_settles_reserved_window(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str, clock: type[_FakeClock]
) -> None:
    """Reserve Sunday 23:59:30, commit Monday 00:00:30: both old windows carry the cost."""
    clock.current = SUN_BEFORE_MONDAY
    rid = await cost_gate.reserve(unique_role, Decimal("1.00"))

    clock.current = MON_AFTER_MIDNIGHT
    await cost_gate.commit(rid, Decimal("0.40"))

    assert await _total(db_pool, unique_role, "daily", SUN_BEFORE_MONDAY) == Decimal("0.40")
    assert await _total(db_pool, unique_role, "weekly", SUN_BEFORE_MONDAY) == Decimal("0.40")
    assert await _total(db_pool, unique_role, "daily", MON_AFTER_MIDNIGHT) is None
    assert await _total(db_pool, unique_role, "weekly", MON_AFTER_MIDNIGHT) is None
    await _assert_no_negative_counter(db_pool, unique_role)


@pytest.mark.asyncio
async def test_refund_after_boundary_frees_reserved_window(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str, clock: type[_FakeClock]
) -> None:
    """A refund after the weekly boundary returns the old windows to zero."""
    clock.current = SUN_BEFORE_MONDAY
    rid = await cost_gate.reserve(unique_role, Decimal("1.00"))

    clock.current = MON_AFTER_MIDNIGHT
    await cost_gate.refund(rid)

    assert await _total(db_pool, unique_role, "daily", SUN_BEFORE_MONDAY) == Decimal("0")
    assert await _total(db_pool, unique_role, "weekly", SUN_BEFORE_MONDAY) == Decimal("0")
    assert await _total(db_pool, unique_role, "daily", MON_AFTER_MIDNIGHT) is None
    assert await _total(db_pool, unique_role, "weekly", MON_AFTER_MIDNIGHT) is None
    await _assert_no_negative_counter(db_pool, unique_role)


@pytest.mark.asyncio
async def test_reaper_after_boundary_frees_reserved_window(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str, clock: type[_FakeClock]
) -> None:
    """The reaper that expires a reservation after the weekly boundary frees the old windows."""
    clock.current = SUN_BEFORE_MONDAY
    rid = await cost_gate.reserve(unique_role, Decimal("1.00"))

    clock.current = MON_AFTER_MIDNIGHT
    await _expire_and_reap(cost_gate, db_pool, rid)

    assert await _total(db_pool, unique_role, "daily", SUN_BEFORE_MONDAY) == Decimal("0")
    assert await _total(db_pool, unique_role, "weekly", SUN_BEFORE_MONDAY) == Decimal("0")
    assert await _total(db_pool, unique_role, "daily", MON_AFTER_MIDNIGHT) is None
    assert await _total(db_pool, unique_role, "weekly", MON_AFTER_MIDNIGHT) is None
    await _assert_no_negative_counter(db_pool, unique_role)


@pytest.mark.asyncio
async def test_late_commit_after_boundary_charges_reserved_window(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str, clock: type[_FakeClock]
) -> None:
    """Both defects together: expired by the reaper, then committed in the next window."""
    clock.current = SUN_BEFORE_MONDAY
    rid = await cost_gate.reserve(unique_role, Decimal("0.40"))

    clock.current = MON_AFTER_MIDNIGHT
    await _expire_and_reap(cost_gate, db_pool, rid)
    await cost_gate.commit(rid, Decimal("0.34"))

    assert await _total(db_pool, unique_role, "daily", SUN_BEFORE_MONDAY) == Decimal("0.34")
    assert await _total(db_pool, unique_role, "weekly", SUN_BEFORE_MONDAY) == Decimal("0.34")
    assert await _total(db_pool, unique_role, "daily", MON_AFTER_MIDNIGHT) is None
    assert await _total(db_pool, unique_role, "weekly", MON_AFTER_MIDNIGHT) is None
    await _assert_no_negative_counter(db_pool, unique_role)


@pytest.mark.asyncio
async def test_row_without_counter_ids_settles_as_before(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str
) -> None:
    """A row written before the migration has NULL counter_ids and falls back to re-derivation."""
    now = datetime.now(timezone.utc)
    rid = await cost_gate.reserve(unique_role, Decimal("1.00"))
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE budget_reservations SET counter_ids = NULL WHERE reservation_id = $1", rid
        )

    await cost_gate.commit(rid, Decimal("0.40"))

    assert await _total(db_pool, unique_role, "daily", now) == Decimal("0.40")
    assert await _total(db_pool, unique_role, "weekly", now) == Decimal("0.40")


# ---------------------------------------------------------------------------
# Review findings (Codex plan review, 2026-10-25)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_commit_and_reaper_running_together_charge_once(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str
) -> None:
    """Whichever of ``commit`` and the reaper wins the row lock, the total is the actual cost."""
    now = datetime.now(timezone.utc)
    rid = await cost_gate.reserve(unique_role, Decimal("0.40"))
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE budget_reservations SET expires_at = NOW() - interval '1 second' "
            "WHERE reservation_id = $1",
            rid,
        )

    await asyncio.gather(cost_gate.commit(rid, Decimal("0.34")), cost_gate.reap_stale())

    assert await _status(db_pool, rid) == ReservationStatus.COMMITTED.value
    assert await _total(db_pool, unique_role, "daily", now) == Decimal("0.34")
    assert await _total(db_pool, unique_role, "weekly", now) == Decimal("0.34")
    await _assert_no_negative_counter(db_pool, unique_role)


@pytest.mark.asyncio
async def test_reserve_locks_counters_in_ascending_id_order(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str
) -> None:
    """A reserve that waits for the lowest-id counter holds no higher-id counter.

    Settlement updates counters in ascending id order. If ``reserve`` locked the
    daily counter (higher id) before the weekly one (lower id), the two could
    deadlock and the aborted ``commit`` would lose the cost.
    """
    now = datetime.now(timezone.utc)
    async with db_pool.acquire() as setup:
        # The weekly row is created first, so its id is lower than the daily row's.
        weekly_id = await setup.fetchval(
            """
            INSERT INTO budget_counters
                (user_id, time_window, provider, role, window_start, running_total)
            VALUES (NULL, 'weekly', NULL, $1, $2, 0) RETURNING id
            """,
            unique_role,
            _window_start("weekly", now),
        )
        daily_id = await setup.fetchval(
            """
            INSERT INTO budget_counters
                (user_id, time_window, provider, role, window_start, running_total)
            VALUES (NULL, 'daily', NULL, $1, $2, 0) RETURNING id
            """,
            unique_role,
            _window_start("daily", now),
        )
    assert weekly_id < daily_id

    async with db_pool.acquire() as holder, db_pool.acquire() as probe:
        tx = holder.transaction()
        await tx.start()
        await holder.execute("SELECT 1 FROM budget_counters WHERE id = $1 FOR UPDATE", weekly_id)
        pending = asyncio.create_task(cost_gate.reserve(unique_role, Decimal("0.10")))
        await asyncio.sleep(0.5)  # let reserve reach the lock it must wait for
        try:
            async with probe.transaction():
                await probe.execute(
                    "SELECT 1 FROM budget_counters WHERE id = $1 FOR UPDATE NOWAIT", daily_id
                )
        finally:
            await tx.commit()
            await pending


@pytest.mark.asyncio
async def test_concurrent_reserve_and_commit_do_not_lose_cost(
    cost_gate: CostGate, db_pool: asyncpg.Pool, unique_role: str
) -> None:
    """Interleaved reserves and commits all succeed and the counters equal the sum of costs."""
    now = datetime.now(timezone.utc)
    first = [await cost_gate.reserve(unique_role, Decimal("0.10")) for _ in range(10)]

    async def reserve_and_commit() -> None:
        rid = await cost_gate.reserve(unique_role, Decimal("0.10"))
        await cost_gate.commit(rid, Decimal("0.05"))

    await asyncio.gather(
        *[cost_gate.commit(rid, Decimal("0.05")) for rid in first],
        *[reserve_and_commit() for _ in range(10)],
    )

    assert await _total(db_pool, unique_role, "daily", now) == Decimal("1.00")
    assert await _total(db_pool, unique_role, "weekly", now) == Decimal("1.00")


@pytest.mark.asyncio
async def test_uncapped_reservation_leaves_placeholder_counter_alone(
    db_pool: asyncpg.Pool, unique_role: str
) -> None:
    """With no caps, ``reserve`` stores no counter ids and settlement mutates no counter."""
    from personal_agent.config import settings

    gate = CostGate(config=BudgetConfig(version=1, roles={}, caps=[]), db_url=settings.database_url)
    await gate.connect()
    try:
        async with db_pool.acquire() as conn:
            before = await conn.fetchval("SELECT SUM(running_total) FROM budget_counters")

        committed = await gate.reserve(unique_role, Decimal("0.30"))
        expired = await gate.reserve(unique_role, Decimal("0.30"))
        await gate.commit(committed, Decimal("0.20"))
        await _expire_and_reap(gate, db_pool, expired)
        await gate.commit(expired, Decimal("0.25"))

        async with db_pool.acquire() as conn:
            after = await conn.fetchval("SELECT SUM(running_total) FROM budget_counters")
            stored = await conn.fetch(
                "SELECT counter_ids, status FROM budget_reservations WHERE role = $1", unique_role
            )
        assert after == before, "an uncapped reservation must not change any counter"
        assert [list(r["counter_ids"]) for r in stored] == [[], []]
        assert {r["status"] for r in stored} == {ReservationStatus.COMMITTED.value}
    finally:
        await gate.disconnect()
