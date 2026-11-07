# ruff: noqa: D102
"""The job record and its lease on the test stack (ADR-0159 D9.1–D9.5; FRE-1684 AC-3).

Each test seeds its own job. Expiry is seeded in the row, because Postgres time is the authority.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest

from personal_agent.dev_agent.jobs import JobState, JobStore
from personal_agent.exceptions import LeaseLostError

from .conftest import MakeJob, expire_lease

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_LEASE = 120.0


async def _noop(conn: asyncpg.Connection) -> str:
    del conn
    return "done"


class TestOneActiveJob:
    async def test_a_second_active_job_is_refused_by_the_index(
        self, make_job: MakeJob, admin_pool: asyncpg.Pool
    ) -> None:
        await make_job()
        with pytest.raises(asyncpg.UniqueViolationError) as refused:
            await make_job()
        assert "dev_jobs_one_active" in str(refused.value)
        active = await admin_pool.fetchval(
            "SELECT count(*) FROM dev_jobs WHERE state IN ('queued','running','checking','delivering')"
        )
        assert active == 1

    async def test_an_ended_job_frees_the_slot(self, make_job: MakeJob, store: JobStore) -> None:
        job = await make_job()
        taken = await store.claim_new("sup-a", _LEASE)
        assert taken is not None
        _, lease = taken
        await store.fenced(
            lease,
            "cleanup",
            lambda conn: store.finish(conn, lease, JobState.FAILED, "test"),
            lease_seconds=_LEASE,
        )
        second = await make_job()
        assert second.state is JobState.QUEUED
        assert (await store.get(job.id)).state is JobState.FAILED  # type: ignore[union-attr]

    async def test_one_approval_creates_one_job(self, make_job: MakeJob, store: JobStore) -> None:
        first = await make_job()
        taken = await store.claim_new("sup-a", _LEASE)
        assert taken is not None
        _, lease = taken
        await store.fenced(
            lease,
            "cleanup",
            lambda c: store.finish(c, lease, JobState.FAILED, "t"),
            lease_seconds=_LEASE,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await make_job(approval_request_id=first.approval_request_id)


class TestLease:
    async def test_claim_bumps_the_epoch_and_a_claimed_job_is_not_claimed_again(
        self, make_job: MakeJob, store: JobStore
    ) -> None:
        job = await make_job()
        taken = await store.claim_new("sup-a", _LEASE)
        assert taken is not None and taken[0].id == job.id and taken[1].epoch == 1
        assert await store.claim_new("sup-b", _LEASE) is None
        assert await store.take_expired("sup-b", _LEASE) is None

    async def test_an_expired_lease_cannot_renew_fence_or_transition(
        self, make_job: MakeJob, store: JobStore, admin_pool: asyncpg.Pool
    ) -> None:
        job = await make_job()
        taken = await store.claim_new("sup-a", _LEASE)
        assert taken is not None
        _, lease = taken
        await expire_lease(admin_pool, job.id)
        assert await store.renew(lease, _LEASE) is None
        with pytest.raises(LeaseLostError):
            await store.fenced(lease, "start_container", _noop, lease_seconds=_LEASE)
        with pytest.raises(LeaseLostError):
            await store.transition(lease, JobState.RUNNING)
        assert (
            await admin_pool.fetchval(
                "SELECT count(*) FROM dev_job_actions WHERE job_id = $1", job.id
            )
            == 0
        )

    async def test_after_a_takeover_the_old_epoch_can_do_nothing(
        self, make_job: MakeJob, store: JobStore, admin_pool: asyncpg.Pool
    ) -> None:
        job = await make_job()
        taken = await store.claim_new("sup-a", _LEASE)
        assert taken is not None
        _, old = taken
        await expire_lease(admin_pool, job.id)
        recovered = await store.take_expired("sup-b", _LEASE)
        assert recovered is not None
        _, new = recovered
        assert new.epoch == old.epoch + 1
        assert await store.renew(old, _LEASE) is None
        with pytest.raises(LeaseLostError):
            await store.fenced(old, "start_container", _noop, lease_seconds=_LEASE)
        assert await store.renew(new, _LEASE) is not None

    async def test_renew_reads_the_token_stop(
        self, make_job: MakeJob, store: JobStore, admin_pool: asyncpg.Pool
    ) -> None:
        job = await make_job()
        taken = await store.claim_new("sup-a", _LEASE)
        assert taken is not None
        _, lease = taken
        assert await store.renew(lease, _LEASE) == (await store.renew(lease, _LEASE))
        await admin_pool.execute(
            """
            INSERT INTO dev_job_tokens (token_hash, job_id, model_key, trace_id, session_id, status,
                                        stop_reason, expires_at)
            VALUES ($1, $2, 'm', $3, $4, 'stopped', 'cap_reached', clock_timestamp() + interval '1 hour')
            """,
            uuid4().hex,
            job.id,
            job.trace_id,
            job.session_id,
        )
        signal = await store.renew(lease, _LEASE)
        assert signal is not None
        assert (signal.status, signal.stop_reason) == ("stopped", "cap_reached")


class TestFence:
    async def test_a_fenced_action_is_logged_with_its_outcome(
        self, make_job: MakeJob, store: JobStore, admin_pool: asyncpg.Pool
    ) -> None:
        job = await make_job()
        taken = await store.claim_new("sup-a", _LEASE)
        assert taken is not None
        _, lease = taken
        assert await store.fenced(lease, "start_container", _noop, lease_seconds=_LEASE) == "done"

        async def broken(conn: asyncpg.Connection) -> None:
            raise RuntimeError("docker failed")

        with pytest.raises(RuntimeError):
            await store.fenced(lease, "remove", broken, lease_seconds=_LEASE)
        rows = await admin_pool.fetch(
            "SELECT action, outcome, lease_epoch FROM dev_job_actions WHERE job_id = $1 ORDER BY id",
            job.id,
        )
        assert [(r["action"], r["outcome"], r["lease_epoch"]) for r in rows] == [
            ("start_container", "done", 1),
            ("remove", "error", 1),
        ]

    async def test_a_database_error_in_an_action_is_raised_and_logged(
        self, make_job: MakeJob, store: JobStore, admin_pool: asyncpg.Pool
    ) -> None:
        """Review finding 3: the real error reaches the caller, and the action row survives."""
        job = await make_job()
        taken = await store.claim_new("sup-a", _LEASE)
        assert taken is not None
        _, lease = taken

        async def bad_sql(conn: asyncpg.Connection) -> None:
            await conn.execute("UPDATE dev_jobs SET task_text = 'changed' WHERE id = $1", job.id)
            await conn.execute("SELECT * FROM no_such_table")

        with pytest.raises(asyncpg.UndefinedTableError):
            await store.fenced(lease, "issue_token", bad_sql, lease_seconds=_LEASE)
        rows = await admin_pool.fetch(
            "SELECT action, outcome FROM dev_job_actions WHERE job_id = $1", job.id
        )
        assert [(r["action"], r["outcome"]) for r in rows] == [("issue_token", "error")]
        assert (await store.get(job.id)).task_text != "changed"  # type: ignore[union-attr]
        assert await store.renew(lease, _LEASE) is not None

    async def test_a_cancelled_action_is_logged(
        self, make_job: MakeJob, store: JobStore, admin_pool: asyncpg.Pool
    ) -> None:
        """Review finding 4: a cancel (gateway stop) keeps the action row, as an error."""
        job = await make_job()
        taken = await store.claim_new("sup-a", _LEASE)
        assert taken is not None
        _, lease = taken
        inside = asyncio.Event()

        async def hangs(conn: asyncpg.Connection) -> None:
            inside.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            store.fenced(lease, "start_container", hangs, lease_seconds=_LEASE)
        )
        await inside.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        rows = await admin_pool.fetch(
            "SELECT action, outcome FROM dev_job_actions WHERE job_id = $1", job.id
        )
        assert [(r["action"], r["outcome"]) for r in rows] == [("start_container", "error")]

    async def test_a_cleanup_longer_than_the_lease_still_writes_the_end_state(
        self, make_job: MakeJob, store: JobStore
    ) -> None:
        """Review finding 1: inside the fence the row lock, not the expiry, proves the lease."""
        job = await make_job()
        taken = await store.claim_new("sup-a", 1.0)
        assert taken is not None
        _, lease = taken

        async def slow_cleanup(conn: asyncpg.Connection) -> None:
            await asyncio.sleep(1.5)
            await store.finish(conn, lease, JobState.TIMED_OUT, "time_limit")

        await store.fenced(lease, "cleanup", slow_cleanup, lease_seconds=1.0)
        assert (await store.get(job.id)).state is JobState.TIMED_OUT  # type: ignore[union-attr]

    async def test_recovery_cannot_take_a_job_while_its_action_runs(
        self, make_job: MakeJob, store: JobStore, admin_pool: asyncpg.Pool
    ) -> None:
        """Codex finding 1: the lease cannot change hands between a fence and its action."""
        job = await make_job()
        taken = await store.claim_new("sup-a", 1.0)
        assert taken is not None
        _, lease = taken
        inside = asyncio.Event()
        release = asyncio.Event()

        async def slow_action(conn: asyncpg.Connection) -> str:
            inside.set()
            await release.wait()
            return "started"

        action = asyncio.create_task(
            store.fenced(lease, "start_container", slow_action, lease_seconds=1.0)
        )
        await inside.wait()
        await asyncio.sleep(1.5)
        # The lease expired while the action runs, but the action holds the row: recovery skips it.
        assert await admin_pool.fetchval(
            "SELECT lease_expires_at < clock_timestamp() FROM dev_jobs WHERE id = $1", job.id
        )
        assert await store.take_expired("sup-b", _LEASE) is None
        release.set()
        assert await action == "started"
        # The holder kept the lock, so the fence extended its lease again after the action.
        assert await store.take_expired("sup-b", _LEASE) is None
        assert await store.renew(lease, 1.0) is not None
        await asyncio.sleep(1.2)
        recovered = await store.take_expired("sup-b", _LEASE)
        assert recovered is not None and recovered[1].epoch == 2

    async def test_mark_started_uses_database_time(
        self, make_job: MakeJob, store: JobStore, admin_pool: asyncpg.Pool
    ) -> None:
        job = await make_job()
        taken = await store.claim_new("sup-a", _LEASE)
        assert taken is not None
        _, lease = taken
        assert await store.seconds_left(job.id, 1800) == 1800
        await store.fenced(lease, "start_container", _noop, lease_seconds=_LEASE, mark_started=True)
        assert 1790 < await store.seconds_left(job.id, 1800) <= 1800
        await admin_pool.execute(
            "UPDATE dev_jobs SET started_at = clock_timestamp() - interval '31 minutes' WHERE id = $1",
            job.id,
        )
        assert await store.seconds_left(job.id, 1800) < 0


class TestOrphans:
    async def test_only_ended_or_missing_jobs_are_orphans(
        self, make_job: MakeJob, store: JobStore
    ) -> None:
        live = await make_job()
        missing = uuid4()
        assert await store.ended_or_missing([live.id, missing]) == {missing}
