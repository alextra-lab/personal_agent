# ruff: noqa: D101, D102
"""The job supervisor on the real job store (ADR-0159 D9.4–D9.7; FRE-1684 AC-1, AC-2, AC-4).

The store runs on the test stack. Docker, the agent and the pipeline are fakes that record every
call. A manual clock drives the keeper with the ADR's numbers (renew 30 s, stop after 60 s). Lease
expiry and the 30-minute limit use Postgres time, so the tests seed past instants in the row.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from personal_agent.delegation.protocols import DelegationOutcome, DelegationPackage
from personal_agent.dev_agent.jobs import JobStore
from personal_agent.dev_agent.supervisor import JobSupervisor, SupervisorPolicy

from .conftest import MakeJob, expire_lease
from .supervisor_fakes import (
    FailingRenewStore,
    FakeLauncher,
    ManualClock,
    PausingStore,
    RecordingPipeline,
    adapter_factory,
    block_forever,
    succeed,
    until,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class Harness:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool
        self.docker = FakeLauncher()
        self.timeouts: list[float] = []
        self.supervisors: list[JobSupervisor] = []

    def supervisor(
        self,
        owner: str,
        *,
        behaviour=block_forever,  # type: ignore[no-untyped-def]
        store: JobStore | None = None,
        pipeline: RecordingPipeline | None = None,
        policy: SupervisorPolicy | None = None,
        clock: ManualClock | None = None,
    ) -> JobSupervisor:
        sup = JobSupervisor(
            store=store or JobStore(self.pool),
            launcher=self.docker,
            adapter_factory=adapter_factory(behaviour, self.timeouts),
            pipeline=pipeline or RecordingPipeline(),
            policy=policy,
            clock=clock or ManualClock(),
            owner=owner,
        )
        self.supervisors.append(sup)
        return sup

    async def row(self, job_id: UUID) -> asyncpg.Record:
        return await self.pool.fetchrow(
            "SELECT state, failure_reason, lease_epoch, lease_owner, started_at, ended_at "
            "FROM dev_jobs WHERE id = $1",
            job_id,
        )

    async def token(self, job_id: UUID) -> str | None:
        return await self.pool.fetchval(
            "SELECT status FROM dev_job_tokens WHERE job_id = $1", job_id
        )

    async def actions(self, job_id: UUID) -> list[tuple[str, int, str, str | None]]:
        rows = await self.pool.fetch(
            "SELECT action, lease_epoch, lease_owner, outcome FROM dev_job_actions "
            "WHERE job_id = $1 ORDER BY id",
            job_id,
        )
        return [(r["action"], r["lease_epoch"], r["lease_owner"], r["outcome"]) for r in rows]


@pytest_asyncio.fixture
async def h(admin_pool: asyncpg.Pool) -> AsyncIterator[Harness]:
    harness = Harness(admin_pool)
    yield harness
    for sup in harness.supervisors:
        await sup.stop()


async def _wait_end(sup: JobSupervisor) -> None:
    assert sup._current is not None
    await asyncio.wait_for(asyncio.shield(sup._current), timeout=20)


class TestRestart:
    async def test_a_restart_just_after_a_renewal_ends_the_job_after_its_lease_expires(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        """AC-1: failed/supervisor_restart, no container or volume, token revoked."""
        job = await make_job()
        clock = ManualClock()
        a = h.supervisor("sup-a", clock=clock)
        await a.scan_once()
        await until(lambda: len(h.timeouts) == 1)  # the agent runs; no fenced action is open
        await clock.advance(30)
        await until(lambda: a.keeper is not None and a.keeper.renewals == 1)
        await a.stop()  # the gateway restarts: no cleanup runs
        assert (await h.row(job.id))["state"] == "running"
        assert job.id in h.docker.containers and await h.token(job.id) == "active"

        b = h.supervisor("sup-b")
        await b.scan_once()  # the lease has not expired yet: nothing happens
        assert (await h.row(job.id))["state"] == "running"
        assert job.id in h.docker.containers

        await expire_lease(h.pool, job.id)
        await b.scan_once()
        row = await h.row(job.id)
        assert (row["state"], row["failure_reason"]) == ("failed", "supervisor_restart")
        assert row["lease_epoch"] == 2 and row["ended_at"] is not None
        assert job.id not in h.docker.containers
        assert h.docker.ops(job.id)[-1] == "remove"
        assert await h.token(job.id) == "revoked"
        assert (await h.actions(job.id))[-1] == ("cleanup", 2, "sup-b", "done")


class TestPausedSupervisor:
    async def test_a_supervisor_paused_before_a_fence_does_nothing_after_it_resumes(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        """AC-2: no container start and no write after the resume."""
        job = await make_job()
        paused_store = PausingStore(h.pool, pause_before="start_container")
        a = h.supervisor("sup-a", store=paused_store)
        await a.scan_once()
        await until(paused_store.reached.is_set)

        await expire_lease(h.pool, job.id)
        b = h.supervisor("sup-b")
        await b.scan_once()
        assert (await h.row(job.id))["state"] == "failed"
        rows_before = await h.actions(job.id)
        token_before = await h.token(job.id)
        calls_before = list(h.docker.calls)

        paused_store.gate.set()  # A resumes
        await _wait_end(a)
        assert "start" not in h.docker.ops(job.id)
        assert h.docker.calls == calls_before
        assert await h.actions(job.id) == rows_before
        assert [r[2] for r in rows_before] == ["sup-a", "sup-b"]  # issue_token, then B's cleanup
        assert await h.token(job.id) == token_before == "revoked"
        row = await h.row(job.id)
        assert (row["state"], row["lease_epoch"], row["lease_owner"]) == ("failed", 2, "sup-b")

    async def test_recovery_waits_for_an_action_in_progress_and_its_cleanup_removes_it(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        """Codex finding 1: a pause between the fence and the action cannot outlive a takeover."""
        job = await make_job()
        h.docker.start_gate = asyncio.Event()
        a = h.supervisor("sup-a", policy=SupervisorPolicy(lease_seconds=1.0))
        await a.scan_once()
        await h.docker.inside_start.wait()
        await asyncio.sleep(1.5)  # A's lease expires while its action holds the row

        b = h.supervisor("sup-b")
        await b.scan_once()
        assert (await h.row(job.id))["state"] == "running", "recovery must skip a locked job"

        h.docker.start_gate.set()
        await until(lambda: "start" in h.docker.ops(job.id))
        await a.stop()
        await asyncio.sleep(1.2)  # the lease that the fence extended expires
        await b.scan_once()
        assert h.docker.ops(job.id) == ["start", "remove"]
        assert job.id not in h.docker.containers
        assert (await h.row(job.id))["state"] == "failed"


class TestLimits:
    async def test_a_job_past_30_minutes_reaches_the_handoff_and_ends_timed_out(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        """AC-4: the handoff runs, the end state is timed_out, and no container is left."""
        job = await make_job()

        async def runs_past_the_limit(
            package: DelegationPackage, timeout: float
        ) -> DelegationOutcome:
            # Seed 31 minutes of run time, then end as the adapter does at its deadline.
            await h.pool.execute(
                "UPDATE dev_jobs SET started_at = clock_timestamp() - interval '31 minutes' "
                "WHERE id = $1",
                job.id,
            )
            return DelegationOutcome(package.task_id, False, 3, "", "timed_out")

        pipeline = RecordingPipeline(result=None)
        a = h.supervisor("sup-a", behaviour=runs_past_the_limit, pipeline=pipeline)
        await a.scan_once()
        await _wait_end(a)
        assert 1790 < h.timeouts[0] <= 1800, "the agent gets the time left of the 30 minutes"
        assert pipeline.calls == ["handoff"]
        row = await h.row(job.id)
        assert (row["state"], row["failure_reason"]) == ("timed_out", "time_limit")
        assert h.docker.ops(job.id) == ["start", "stop", "remove"]
        assert job.id not in h.docker.containers
        assert await h.token(job.id) == "revoked"

    async def test_the_limit_counts_from_the_container_start(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        job = await make_job()
        a = h.supervisor("sup-a", behaviour=succeed)
        await a.scan_once()
        await _wait_end(a)
        row = await h.row(job.id)
        assert row["started_at"] is not None
        assert row["ended_at"] - row["started_at"] < timedelta(seconds=30)
        assert [r[0] for r in await h.actions(job.id)] == [
            "issue_token",
            "start_container",
            "revoke_token",
            "stop_agent",
            "cleanup",
        ]

    async def test_a_stopped_token_ends_the_job_with_its_reason(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        job = await make_job()
        clock = ManualClock()
        a = h.supervisor("sup-a", clock=clock)
        await a.scan_once()
        await until(lambda: len(h.timeouts) == 1)
        await h.pool.execute(
            "UPDATE dev_job_tokens SET status = 'stopped', stop_reason = 'cap_reached' WHERE job_id = $1",
            job.id,
        )
        await clock.advance(30)
        await _wait_end(a)
        row = await h.row(job.id)
        assert (row["state"], row["failure_reason"]) == ("cap_reached", "cap_reached")
        assert job.id not in h.docker.containers
        assert await h.token(job.id) == "revoked"

    async def test_sixty_seconds_without_a_renewal_stops_all_work(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        job = await make_job()
        clock = ManualClock()
        a = h.supervisor("sup-a", clock=clock, store=FailingRenewStore(h.pool))
        await a.scan_once()
        await until(lambda: len(h.timeouts) == 1)  # the agent runs; no fenced action is open
        await clock.advance(30)
        assert a.busy, "one failed renewal (30 s) does not stop the work"
        await clock.advance(30)
        await until(lambda: not a.busy)
        assert a.keeper is not None and a.keeper.lost
        assert h.docker.ops(job.id) == ["start"], "a stale supervisor does no cleanup"
        assert (await h.row(job.id))["state"] == "running"


class TestCleanup:
    async def test_a_failed_cleanup_keeps_the_job_in_the_slot_until_recovery_cleans_it(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        job = await make_job()
        h.docker.fail_remove = 2  # the end and the error path both fail
        a = h.supervisor("sup-a", behaviour=succeed)
        await a.scan_once()
        await _wait_end(a)
        assert (await h.row(job.id))["state"] == "running"
        with pytest.raises(asyncpg.UniqueViolationError):
            await make_job()

        await expire_lease(h.pool, job.id)
        await h.supervisor("sup-b").scan_once()
        row = await h.row(job.id)
        assert (row["state"], row["failure_reason"]) == ("failed", "supervisor_restart")
        assert job.id not in h.docker.containers
        assert await h.token(job.id) == "revoked"

    async def test_the_orphan_sweep_removes_only_ended_or_unknown_jobs(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        live = await make_job()
        unknown = UUID(int=7)
        h.docker.containers |= {live.id, unknown}
        await h.supervisor("sup-b").sweep_orphans()
        assert h.docker.containers == {live.id}


class TestResult:
    async def test_a_result_that_passes_is_checked_delivered_under_the_fence_and_succeeds(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        job = await make_job()
        pipeline = RecordingPipeline(result="abc123")
        a = h.supervisor("sup-a", behaviour=succeed, pipeline=pipeline)
        await a.scan_once()
        await _wait_end(a)
        assert pipeline.calls == ["handoff", "check abc123", "deliver", "upload"]
        assert (await h.row(job.id))["state"] == "succeeded"
        assert ("upload", 1, "sup-a", "done") in await h.actions(job.id)
        assert job.id not in h.docker.containers

    async def test_a_result_that_fails_the_check_ends_failed_check(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        job = await make_job()
        pipeline = RecordingPipeline(result="abc123", passes=False)
        a = h.supervisor("sup-a", behaviour=succeed, pipeline=pipeline)
        await a.scan_once()
        await _wait_end(a)
        assert pipeline.calls == ["handoff", "check abc123"]
        assert (await h.row(job.id))["state"] == "failed_check"

    async def test_a_delivering_job_resumes_its_delivery_after_a_restart(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        job = await make_job()
        first = RecordingPipeline(result="abc123", deliver_gate=asyncio.Event())
        a = h.supervisor("sup-a", behaviour=succeed, pipeline=first)
        await a.scan_once()
        await first.in_delivery.wait()
        await a.stop()
        assert (await h.row(job.id))["state"] == "delivering"

        await expire_lease(h.pool, job.id)
        second = RecordingPipeline(result="abc123")
        b = h.supervisor("sup-b", pipeline=second)
        await b.scan_once()
        await _wait_end(b)
        assert second.calls == ["deliver", "upload"]
        row = await h.row(job.id)
        assert (row["state"], row["lease_epoch"]) == ("succeeded", 2)
        assert job.id not in h.docker.containers

