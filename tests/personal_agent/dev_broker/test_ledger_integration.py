# ruff: noqa: D102
"""The job and month counters (ADR-0159 D5.4-D5.9; FRE-1683 AC-1, AC-2, AC-3).

Runs against the test stack as the broker's own role. Every test uses its own job and a month far
from today, so no test shares a counter row with another test or with production data.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio
from personal_agent.dev_broker.ledger import (
    Admitted,
    Cancelled,
    CostUnknown,
    Failed,
    ProviderError,
    Refusal,
    Settled,
    SpendLedger,
    TimedOut,
)
from personal_agent.dev_broker.tokens import hash_token, issue_job_token, revoke_job_token

from .conftest import delete_job_rows

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_MODEL = "deepseek-v4.1-flash-scaleway"
_JOB_CAP = Decimal("2.36")
_MONTH_CAP = Decimal("35.40")


def _far_month() -> datetime:
    """A noon instant in a random far-future month, unique to this test."""
    return datetime(random.randint(2200, 9000), random.randint(1, 12), 15, 12, tzinfo=timezone.utc)


class _Job:
    def __init__(self, job_id: UUID, token: str, now: datetime) -> None:
        self.job_id = job_id
        self.token = token
        self.token_hash = hash_token(token)
        self.now = now


@pytest_asyncio.fixture
async def job(admin_pool: asyncpg.Pool) -> AsyncIterator[_Job]:
    """An active job token, valid for a day around a far-future instant."""
    now = _far_month()
    job_id = uuid4()
    token = await issue_job_token(
        admin_pool,
        job_id=job_id,
        model_key=_MODEL,
        trace_id=uuid4(),
        session_id=uuid4(),
        expires_at=now + timedelta(days=60),
    )
    try:
        yield _Job(job_id, token, now)
    finally:
        await delete_job_rows(admin_pool, job_id)
        async with admin_pool.acquire() as conn:
            for months in range(0, 3):
                start = (now.replace(day=1) + timedelta(days=32 * months)).replace(day=1).date()
                await conn.execute("DELETE FROM dev_month_spend WHERE month_start = $1", start)


def _ledger(pool: asyncpg.Pool, *, job_cap: Decimal = _JOB_CAP) -> SpendLedger:
    return SpendLedger(pool, job_cap_usd=job_cap, month_cap_usd=_MONTH_CAP)


async def _reserve(ledger: SpendLedger, job: _Job, amount: str, now: datetime | None = None):  # type: ignore[no-untyped-def]
    return await ledger.reserve(
        token_hash=job.token_hash,
        model_key=_MODEL,
        amount_usd=Decimal(amount),
        prompt_token_bound=100,
        max_output_tokens=50,
        now=now or job.now,
    )


async def _spend(pool: asyncpg.Pool, job_id: UUID) -> tuple[Decimal, Decimal]:
    row = await pool.fetchrow(
        "SELECT committed_usd, reserved_usd FROM dev_job_spend WHERE job_id = $1", job_id
    )
    assert row is not None
    return row["committed_usd"], row["reserved_usd"]


async def _month(pool: asyncpg.Pool, start: date) -> tuple[Decimal, Decimal] | None:
    row = await pool.fetchrow(
        "SELECT committed_usd, reserved_usd FROM dev_month_spend WHERE month_start = $1", start
    )
    return None if row is None else (row["committed_usd"], row["reserved_usd"])


async def _token(pool: asyncpg.Pool, job_id: UUID) -> tuple[str, str | None]:
    row = await pool.fetchrow(
        "SELECT status, stop_reason FROM dev_job_tokens WHERE job_id = $1", job_id
    )
    assert row is not None
    return row["status"], row["stop_reason"]


class TestReserve:
    async def test_admits_and_reserves_on_both_counters(
        self, broker_pool: asyncpg.Pool, job: _Job
    ) -> None:
        result = await _reserve(_ledger(broker_pool), job, "0.10")

        assert isinstance(result, Admitted)
        assert result.job_id == job.job_id
        assert await _spend(broker_pool, job.job_id) == (Decimal("0"), Decimal("0.100000"))
        assert await _month(broker_pool, job.now.date().replace(day=1)) == (
            Decimal("0"),
            Decimal("0.100000"),
        )

    async def test_refuses_past_the_job_cap_and_stops_the_token(
        self, broker_pool: asyncpg.Pool, job: _Job
    ) -> None:
        ledger = _ledger(broker_pool, job_cap=Decimal("0.15"))
        first = await _reserve(ledger, job, "0.10")
        assert isinstance(first, Admitted)
        await ledger.settle(first, Settled(Decimal("0.10"), 1, 1), now=job.now)

        refused = await _reserve(ledger, job, "0.10")

        assert isinstance(refused, Refusal)
        assert refused.reason == "cap_reached"
        assert await _token(broker_pool, job.job_id) == ("stopped", "cap_reached")
        assert await _spend(broker_pool, job.job_id) == (Decimal("0.100000"), Decimal("0"))

    async def test_a_refusal_caused_by_calls_in_flight_does_not_stop_the_token(
        self, broker_pool: asyncpg.Pool, job: _Job
    ) -> None:
        """In-flight worst cases mostly come back, so the job may retry once they settle."""
        ledger = _ledger(broker_pool, job_cap=Decimal("0.15"))
        first = await _reserve(ledger, job, "0.10")
        assert isinstance(first, Admitted)

        busy = await _reserve(ledger, job, "0.10")

        assert isinstance(busy, Refusal)
        assert busy.reason == "cap_busy"
        assert await _token(broker_pool, job.job_id) == ("active", None)
        await ledger.settle(first, Settled(Decimal("0.01"), 1, 1), now=job.now)
        assert isinstance(await _reserve(ledger, job, "0.10"), Admitted)
        calls = await broker_pool.fetchval(
            "SELECT count(*) FROM dev_job_calls WHERE job_id = $1", job.job_id
        )
        assert calls == 2  # the first and the third: a refusal writes no call row

    async def test_a_stopped_token_is_refused(self, broker_pool: asyncpg.Pool, job: _Job) -> None:
        ledger = _ledger(broker_pool, job_cap=Decimal("0.05"))
        await _reserve(ledger, job, "0.10")

        refused = await _reserve(_ledger(broker_pool), job, "0.01")

        assert isinstance(refused, Refusal)
        assert refused.reason == "inactive_token"

    async def test_refuses_past_the_month_cap(
        self, broker_pool: asyncpg.Pool, admin_pool: asyncpg.Pool, job: _Job
    ) -> None:
        start = job.now.date().replace(day=1)
        await admin_pool.execute(
            "INSERT INTO dev_month_spend (month_start, committed_usd) VALUES ($1, $2)",
            start,
            _MONTH_CAP - Decimal("0.05"),
        )

        refused = await _reserve(_ledger(broker_pool), job, "0.10")

        assert isinstance(refused, Refusal)
        assert refused.reason == "cap_reached"
        assert await _token(broker_pool, job.job_id) == ("stopped", "cap_reached")

    async def test_ten_parallel_reservations_never_pass_the_cap(
        self, broker_pool: asyncpg.Pool, job: _Job
    ) -> None:
        cap = Decimal("0.05") * Decimal("1.18")  # the EUR 0.05 equivalent: USD 0.059
        ledger = _ledger(broker_pool, job_cap=cap)

        results = await asyncio.gather(*(_reserve(ledger, job, "0.008") for _ in range(10)))

        admitted = [r for r in results if isinstance(r, Admitted)]
        assert len(admitted) == 7  # 7 x 0.008 = 0.056 <= 0.059 < 8 x 0.008
        _, reserved = await _spend(broker_pool, job.job_id)
        assert reserved == Decimal("0.008") * len(admitted)
        assert reserved <= cap


class TestTokenChecks:
    async def test_unknown_token(self, broker_pool: asyncpg.Pool, job: _Job) -> None:
        result = await _ledger(broker_pool).check_token(
            token_hash=hash_token("not-a-token"), model_key=_MODEL, now=job.now
        )
        assert isinstance(result, Refusal)
        assert result.reason == "unknown_token"

    async def test_wrong_model(self, broker_pool: asyncpg.Pool, job: _Job) -> None:
        result = await _ledger(broker_pool).check_token(
            token_hash=job.token_hash, model_key="glm-5.3-scaleway", now=job.now
        )
        assert isinstance(result, Refusal)
        assert result.reason == "wrong_model"

    async def test_expired(self, broker_pool: asyncpg.Pool, job: _Job) -> None:
        later = job.now + timedelta(days=61)
        result = await _reserve(_ledger(broker_pool), job, "0.01", now=later)
        assert isinstance(result, Refusal)
        assert result.reason == "expired_token"

    async def test_revoked(
        self, broker_pool: asyncpg.Pool, admin_pool: asyncpg.Pool, job: _Job
    ) -> None:
        await revoke_job_token(admin_pool, job.job_id)
        result = await _reserve(_ledger(broker_pool), job, "0.01")
        assert isinstance(result, Refusal)
        assert result.reason == "inactive_token"


class TestSettle:
    async def test_settled_moves_reserved_to_committed(
        self, broker_pool: asyncpg.Pool, job: _Job
    ) -> None:
        ledger = _ledger(broker_pool)
        admitted = await _reserve(ledger, job, "0.10")
        assert isinstance(admitted, Admitted)

        await ledger.settle(admitted, Settled(Decimal("0.03"), 90, 40), now=job.now)

        assert await _spend(broker_pool, job.job_id) == (Decimal("0.030000"), Decimal("0"))
        row = await broker_pool.fetchrow(
            "SELECT status, settled_usd, input_tokens, output_tokens FROM dev_job_calls "
            "WHERE call_id = $1",
            admitted.call_id,
        )
        assert dict(row) == {
            "status": "settled",
            "settled_usd": Decimal("0.030000"),
            "input_tokens": 90,
            "output_tokens": 40,
        }

    async def test_missing_usage_keeps_the_worst_case_and_stops_the_token(
        self, broker_pool: asyncpg.Pool, job: _Job
    ) -> None:
        """AC-2: it does not settle at zero, and the token does not stay active."""
        ledger = _ledger(broker_pool)
        admitted = await _reserve(ledger, job, "0.10")
        assert isinstance(admitted, Admitted)

        await ledger.settle(admitted, CostUnknown(), now=job.now)

        assert await _spend(broker_pool, job.job_id) == (Decimal("0.100000"), Decimal("0"))
        assert await _token(broker_pool, job.job_id) == ("stopped", "cost_unknown")

    async def test_a_cost_above_the_worst_case_is_kept_and_stops_the_token(
        self, broker_pool: asyncpg.Pool, job: _Job
    ) -> None:
        ledger = _ledger(broker_pool)
        admitted = await _reserve(ledger, job, "0.10")
        assert isinstance(admitted, Admitted)

        await ledger.settle(admitted, Settled(Decimal("0.12"), 9000, 50), now=job.now)

        assert await _spend(broker_pool, job.job_id) == (Decimal("0.120000"), Decimal("0"))
        assert await _token(broker_pool, job.job_id) == ("stopped", "cost_unknown")

    @pytest.mark.parametrize(
        ("outcome", "status", "spent"),
        [
            (TimedOut(), "timed_out", Decimal("0.100000")),
            (Cancelled(), "cancelled", Decimal("0.100000")),
            (ProviderError(), "provider_error", Decimal("0.100000")),
            (Failed(), "failed", Decimal("0")),
        ],
    )
    async def test_other_outcomes(
        self,
        broker_pool: asyncpg.Pool,
        job: _Job,
        outcome: TimedOut | Cancelled | ProviderError | Failed,
        status: str,
        spent: Decimal,
    ) -> None:
        ledger = _ledger(broker_pool)
        admitted = await _reserve(ledger, job, "0.10")
        assert isinstance(admitted, Admitted)

        await ledger.settle(admitted, outcome, now=job.now)

        assert await _spend(broker_pool, job.job_id) == (spent, Decimal("0"))
        assert (
            await broker_pool.fetchval(
                "SELECT status FROM dev_job_calls WHERE call_id = $1", admitted.call_id
            )
            == status
        )
        assert await _token(broker_pool, job.job_id) == ("active", None)

    async def test_a_second_settle_changes_nothing(
        self, broker_pool: asyncpg.Pool, job: _Job
    ) -> None:
        ledger = _ledger(broker_pool)
        admitted = await _reserve(ledger, job, "0.10")
        assert isinstance(admitted, Admitted)
        await ledger.settle(admitted, Settled(Decimal("0.03"), 1, 1), now=job.now)

        await ledger.settle(admitted, CostUnknown(), now=job.now)

        assert await _spend(broker_pool, job.job_id) == (Decimal("0.030000"), Decimal("0"))
        assert await _token(broker_pool, job.job_id) == ("active", None)


class TestMonthBoundary:
    async def test_spend_reserved_at_23_59_counts_in_that_month(
        self, broker_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
    ) -> None:
        """AC-3, with a fake clock: the last minute of a month, then the first of the next."""
        year = random.randint(2200, 9000)
        last_minute = datetime(year, 11, 30, 23, 59, tzinfo=timezone.utc)
        next_month = datetime(year, 12, 1, 0, 1, tzinfo=timezone.utc)
        november, december = date(year, 11, 1), date(year, 12, 1)
        job_id = uuid4()
        token = await issue_job_token(
            admin_pool,
            job_id=job_id,
            model_key=_MODEL,
            trace_id=uuid4(),
            session_id=uuid4(),
            expires_at=next_month + timedelta(days=2),
        )
        ledger = _ledger(broker_pool)
        try:
            admitted = await ledger.reserve(
                token_hash=hash_token(token),
                model_key=_MODEL,
                amount_usd=Decimal("0.20"),
                prompt_token_bound=1,
                max_output_tokens=1,
                now=last_minute,
            )
            assert isinstance(admitted, Admitted)
            # It settles after midnight, against the month it was reserved in.
            await ledger.settle(admitted, Settled(Decimal("0.05"), 1, 1), now=next_month)

            assert await _month(broker_pool, november) == (Decimal("0.050000"), Decimal("0"))
            assert await _month(broker_pool, december) is None

            second = await ledger.reserve(
                token_hash=hash_token(token),
                model_key=_MODEL,
                amount_usd=Decimal("0.07"),
                prompt_token_bound=1,
                max_output_tokens=1,
                now=next_month,
            )
            assert isinstance(second, Admitted)
            assert await _month(broker_pool, december) == (Decimal("0"), Decimal("0.070000"))
        finally:
            await delete_job_rows(admin_pool, job_id)
            for start in (november, december):
                await admin_pool.execute(
                    "DELETE FROM dev_month_spend WHERE month_start = $1", start
                )


class TestStaleReservations:
    async def test_a_stale_reservation_is_settled_at_the_worst_case(
        self, broker_pool: asyncpg.Pool, job: _Job
    ) -> None:
        """A settle that failed, or a broker that died, never leaves a reservation for good."""
        ledger = _ledger(broker_pool)
        old = await _reserve(ledger, job, "0.10", now=job.now)
        fresh = await _reserve(ledger, job, "0.05", now=job.now + timedelta(seconds=200))
        assert isinstance(old, Admitted) and isinstance(fresh, Admitted)

        swept = await ledger.settle_stale(
            now=job.now + timedelta(seconds=240), older_than_seconds=210
        )

        assert swept == 1
        statuses = dict(
            await broker_pool.fetch(
                "SELECT call_id, status FROM dev_job_calls WHERE job_id = $1", job.job_id
            )
        )
        assert statuses == {old.call_id: "cancelled", fresh.call_id: "reserved"}
        assert await _spend(broker_pool, job.job_id) == (Decimal("0.100000"), Decimal("0.050000"))

    async def test_a_sweep_with_nothing_stale_changes_nothing(
        self, broker_pool: asyncpg.Pool, job: _Job
    ) -> None:
        ledger = _ledger(broker_pool)
        await _reserve(ledger, job, "0.10")
        assert await ledger.settle_stale(now=job.now, older_than_seconds=210) == 0
        assert await _spend(broker_pool, job.job_id) == (Decimal("0"), Decimal("0.100000"))
