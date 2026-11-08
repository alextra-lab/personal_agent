# ruff: noqa: D101, D102
"""The job supervisor on the real job store (ADR-0159 D9.4–D9.7; FRE-1684 AC-1, AC-2, AC-4).

The store runs on the test stack. Docker, the agent and the pipeline are fakes that record every
call. A manual clock drives the keeper with the ADR's numbers (renew 30 s, stop after 60 s). Lease
expiry and the 30-minute limit use Postgres time, so the tests seed past instants in the row.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from personal_agent.delegation.protocols import DelegationOutcome, DelegationPackage
from personal_agent.dev_agent.job_card import CardContext, JobEventSink, PoolJobEventSink
from personal_agent.dev_agent.jobs import JobStore
from personal_agent.dev_agent.supervisor import JobSupervisor, SupervisorPolicy

from personal_agent.transport.agui.adapter import to_agui_event
from personal_agent.transport.agui.transport import emit_dev_job

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
        events: JobEventSink | None = None,
    ) -> JobSupervisor:
        sup = JobSupervisor(
            store=store or JobStore(self.pool),
            launcher=self.docker,
            adapter_factory=adapter_factory(behaviour, self.timeouts),
            pipeline=pipeline or RecordingPipeline(),
            policy=policy,
            clock=clock or ManualClock(),
            owner=owner,
            events=events,
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
        await a.stop()  # a graceful gateway stop: the token is revoked, nothing else is cleaned
        assert (await h.row(job.id))["state"] == "running"
        assert job.id in h.docker.containers
        assert await h.token(job.id) == "revoked", "review finding 6: no spend after the stop"

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
        # A's issue_token, then B's revoke and cleanup.
        assert [(r[0], r[2]) for r in rows_before] == [
            ("issue_token", "sup-a"),
            ("revoke_token", "sup-b"),
            ("cleanup", "sup-b"),
        ]
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
        assert pipeline.calls == ["prepare", "handoff"]
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
        expiry = await h.pool.fetchval(
            "SELECT expires_at - created_at FROM dev_job_tokens WHERE job_id = $1", job.id
        )
        # Review finding 8: the expiry is Postgres time + limit + grace (1800 + 300 s).
        assert timedelta(seconds=2099) < expiry < timedelta(seconds=2101)
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

    async def test_a_stop_after_the_last_renewal_is_not_lost(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        """Review finding 2: the broker stops the token, and the agent ends before a renewal."""
        job = await make_job()

        async def stopped_then_done(
            package: DelegationPackage, timeout: float
        ) -> DelegationOutcome:
            del timeout
            await h.pool.execute(
                "UPDATE dev_job_tokens SET status = 'stopped', stop_reason = 'cost_unknown' "
                "WHERE job_id = $1",
                job.id,
            )
            return DelegationOutcome(package.task_id, True, 1, "done", "")

        pipeline = RecordingPipeline(result="abc123")
        a = h.supervisor("sup-a", behaviour=stopped_then_done, pipeline=pipeline)
        await a.scan_once()
        await _wait_end(a)
        assert a.keeper is not None and a.keeper.renewals == 0
        row = await h.row(job.id)
        assert (row["state"], row["failure_reason"]) == ("cost_unknown", "cost_unknown")
        assert pipeline.calls == ["prepare"], "a stopped job never reaches the handoff"
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

    async def test_a_failed_end_is_retried_with_its_own_state(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        """Review finding 3: a retry keeps the intended end state and reason."""
        job = await make_job()
        h.docker.fail_remove = 1
        a = h.supervisor("sup-a", behaviour=succeed)
        await a.scan_once()
        await _wait_end(a)
        row = await h.row(job.id)
        assert (row["state"], row["failure_reason"]) == ("failed", "no_result")
        assert h.docker.ops(job.id)[-2:] == ["remove_failed", "remove"]

    async def test_recovering_its_own_stale_job_stops_the_local_run_first(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        """Review finding 5: the local run of a job that lost its lease stops before the cleanup."""
        job = await make_job()
        a = h.supervisor("sup-a")
        await a.scan_once()
        await until(lambda: len(h.timeouts) == 1)
        await expire_lease(h.pool, job.id)
        await a.scan_once()
        assert not a.busy
        row = await h.row(job.id)
        assert (row["state"], row["failure_reason"]) == ("failed", "supervisor_restart")
        assert job.id not in h.docker.containers

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
        assert pipeline.calls == [
            "prepare", "handoff", "check abc123", "deliver", "upload", "advance_main",
        ]  # fmt: skip
        assert pipeline.seen_results == ["abc123"]
        assert h.docker.workspaces[job.id] == b"workspace-tar"
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
        assert pipeline.calls == ["prepare", "handoff", "check abc123"]
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
        assert second.calls == ["deliver", "upload", "advance_main"]
        row = await h.row(job.id)
        assert (row["state"], row["lease_epoch"]) == ("succeeded", 2)
        assert job.id not in h.docker.containers


class TestRepoHooks:
    """The repo steps of FRE-1685 around the agent (ADR-0159 D8.4, D8.8, D8.9)."""

    async def test_a_refused_prepare_ends_failed_with_its_reason_and_starts_nothing(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        job = await make_job()
        pipeline = RecordingPipeline(refuse_prepare="source_not_found")
        a = h.supervisor("sup-a", behaviour=succeed, pipeline=pipeline)
        await a.scan_once()
        await _wait_end(a)
        row = await h.row(job.id)
        assert (row["state"], row["failure_reason"]) == ("failed", "source_not_found")
        assert "start" not in h.docker.ops(job.id)
        assert pipeline.calls == ["prepare"]
        assert await h.token(job.id) is None, "no token is issued before prepare passes"

    async def test_a_refused_handoff_ends_failed_with_its_reason(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        job = await make_job()
        pipeline = RecordingPipeline(refuse_handoff="head_not_descendant")
        a = h.supervisor("sup-a", behaviour=succeed, pipeline=pipeline)
        await a.scan_once()
        await _wait_end(a)
        row = await h.row(job.id)
        assert (row["state"], row["failure_reason"]) == ("failed", "head_not_descendant")
        assert pipeline.calls == ["prepare", "handoff"]

    async def test_advance_main_is_retried_once(self, h: Harness, make_job: MakeJob) -> None:
        job = await make_job()
        pipeline = RecordingPipeline(result="abc123", advance_failures=1)
        a = h.supervisor("sup-a", behaviour=succeed, pipeline=pipeline)
        await a.scan_once()
        await _wait_end(a)
        assert pipeline.calls[-2:] == ["advance_main", "advance_main"]
        assert (await h.row(job.id))["state"] == "succeeded"

    async def test_a_published_page_succeeds_even_when_main_cannot_move(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        job = await make_job()
        pipeline = RecordingPipeline(result="abc123", advance_failures=5)
        a = h.supervisor("sup-a", behaviour=succeed, pipeline=pipeline)
        await a.scan_once()
        await _wait_end(a)
        assert pipeline.calls.count("advance_main") == 2
        assert (await h.row(job.id))["state"] == "succeeded"
        assert await h.pool.fetchval("SELECT main_moved FROM dev_jobs WHERE id = $1", job.id) is None

    async def test_a_refused_handoff_after_the_limit_ends_timed_out(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        job = await make_job()

        async def runs_past_the_limit(
            package: DelegationPackage, timeout: float
        ) -> DelegationOutcome:
            await h.pool.execute(
                "UPDATE dev_jobs SET started_at = clock_timestamp() - interval '31 minutes' "
                "WHERE id = $1",
                job.id,
            )
            return DelegationOutcome(package.task_id, False, 3, "", "timed_out")

        pipeline = RecordingPipeline(refuse_handoff="page_missing")
        a = h.supervisor("sup-a", behaviour=runs_past_the_limit, pipeline=pipeline)
        await a.scan_once()
        await _wait_end(a)
        row = await h.row(job.id)
        assert (row["state"], row["failure_reason"]) == ("timed_out", "page_missing")

    async def test_a_refused_check_keeps_its_reason(self, h: Harness, make_job: MakeJob) -> None:
        """Code review finding 2: every step's refusal keeps its reason."""
        job = await make_job()
        pipeline = RecordingPipeline(result="abc123", refuse_check="check_refused_reason")
        a = h.supervisor("sup-a", behaviour=succeed, pipeline=pipeline)
        await a.scan_once()
        await _wait_end(a)
        row = await h.row(job.id)
        assert (row["state"], row["failure_reason"]) == ("failed", "check_refused_reason")
        assert job.id not in h.docker.containers


class CheckWritingPipeline(RecordingPipeline):
    """A pipeline whose failed check leaves a ``check_result`` row, as the real check does."""

    def __init__(self, pool: asyncpg.Pool, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._pool = pool

    async def check(self, job, result_commit, fence):  # type: ignore[no-untyped-def]
        await self._pool.execute(
            "UPDATE dev_jobs SET check_result = $2::jsonb WHERE id = $1",
            job.id,
            json.dumps({"passed": False, "steps": [{"name": "load", "passed": False}]}),
        )
        return await super().check(job, result_commit, fence)


class TestEvents:
    """FRE-1688 (ADR-0159 D10.6, AC-7): the real sink and ``emit_dev_job``; ``_push_event`` is captured."""

    @pytest.fixture
    def pushed(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[dict[str, object], str]]:
        captured: list[tuple[dict[str, object], str]] = []

        async def push(event: object, session_id: str) -> bool:
            captured.append((to_agui_event(event), session_id))  # type: ignore[arg-type]
            return True

        monkeypatch.setattr("personal_agent.transport.agui.transport._push_event", push)
        return captured

    def _sink(self, h: Harness) -> PoolJobEventSink:
        return PoolJobEventSink(
            h.pool,
            emit_dev_job,
            context=CardContext(cap_usd=Decimal("2.36"), repo_base_url=None),
        )

    @staticmethod
    def _cards(pushed: list[tuple[dict[str, object], str]]) -> list[dict[str, object]]:
        return [e["data"]["value"] for e, _ in pushed if e["data"]["key"] == "dev_job"]  # type: ignore[index]

    async def test_a_restart_end_state_reaches_the_session(
        self, h: Harness, make_job: MakeJob, pushed: list[tuple[dict[str, object], str]]
    ) -> None:
        job = await make_job()
        clock = ManualClock()
        a = h.supervisor("sup-a", clock=clock)
        await a.scan_once()
        await until(lambda: len(h.timeouts) == 1)
        await a.stop()
        assert pushed == []  # the first supervisor has no sink

        await expire_lease(h.pool, job.id)
        b = h.supervisor("sup-b", events=self._sink(h))
        await b.scan_once()

        assert [sid for _, sid in pushed] == [str(job.session_id)]
        envelope, _ = pushed[0]
        assert envelope["type"] == "STATE_DELTA"
        card = envelope["data"]["value"]  # type: ignore[index]
        assert (card["event"], card["state"], card["failure_reason"], card["ended"]) == (
            "end",
            "failed",
            "supervisor_restart",
            True,
        )
        assert card["job_id"] == str(job.id)

    async def test_a_job_that_succeeds_sends_each_state_then_the_end(
        self, h: Harness, make_job: MakeJob, pushed: list[tuple[dict[str, object], str]]
    ) -> None:
        await make_job()
        pipeline = RecordingPipeline(result="abc123")
        a = h.supervisor("sup-a", behaviour=succeed, pipeline=pipeline, events=self._sink(h))
        await a.scan_once()
        await _wait_end(a)
        cards = self._cards(pushed)
        assert [(c["event"], c["state"]) for c in cards] == [
            ("state", "running"),
            ("state", "checking"),
            ("state", "delivering"),
            ("end", "succeeded"),
        ]

    async def test_a_failed_check_sends_its_result_as_an_object(
        self, h: Harness, make_job: MakeJob, pushed: list[tuple[dict[str, object], str]]
    ) -> None:
        await make_job()
        pipeline = CheckWritingPipeline(h.pool, result="abc123", passes=False)
        a = h.supervisor("sup-a", behaviour=succeed, pipeline=pipeline, events=self._sink(h))
        await a.scan_once()
        await _wait_end(a)
        end = self._cards(pushed)[-1]
        assert (end["event"], end["state"], end["failure_reason"]) == (
            "end",
            "failed_check",
            "check_failed",
        )
        assert end["check_result"] == {"passed": False, "steps": [{"name": "load", "passed": False}]}

    async def test_a_refused_prepare_sends_its_reason(
        self, h: Harness, make_job: MakeJob, pushed: list[tuple[dict[str, object], str]]
    ) -> None:
        await make_job()
        pipeline = RecordingPipeline(refuse_prepare="source_not_found")
        a = h.supervisor("sup-a", pipeline=pipeline, events=self._sink(h))
        await a.scan_once()
        await _wait_end(a)
        end = self._cards(pushed)[-1]
        assert (end["state"], end["failure_reason"]) == ("failed", "source_not_found")

    async def test_the_end_card_spend_equals_the_row(
        self, h: Harness, make_job: MakeJob, pushed: list[tuple[dict[str, object], str]]
    ) -> None:
        job = await make_job()
        await h.pool.execute(
            "INSERT INTO dev_job_spend (job_id, committed_usd) VALUES ($1, 0.250000)", job.id
        )
        a = h.supervisor(
            "sup-a",
            behaviour=succeed,
            pipeline=RecordingPipeline(result="abc123"),
            events=self._sink(h),
        )
        await a.scan_once()
        await _wait_end(a)
        row_spend = await h.pool.fetchval("SELECT spend_usd::text FROM dev_jobs WHERE id = $1", job.id)
        assert row_spend == "0.250000"
        assert self._cards(pushed)[-1]["spend_usd"] == row_spend

    async def test_a_sink_that_cannot_send_does_not_change_the_job(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        job = await make_job()

        async def broken(*, session_id: str, payload: dict[str, object]) -> bool:
            raise ConnectionError("seeded: transport down")

        sink = PoolJobEventSink(h.pool, broken, context=CardContext(None, None))
        a = h.supervisor(
            "sup-a", behaviour=succeed, pipeline=RecordingPipeline(result="abc123"), events=sink
        )
        await a.scan_once()
        await _wait_end(a)
        assert (await h.row(job.id))["state"] == "succeeded"

    async def test_a_sink_that_hangs_does_not_stall_the_job(
        self, h: Harness, make_job: MakeJob
    ) -> None:
        job = await make_job()

        class HangingSink:
            async def notify(self, job_id: UUID, event: str, step: str | None = None) -> None:
                await asyncio.Event().wait()

        a = h.supervisor(
            "sup-a",
            behaviour=succeed,
            pipeline=RecordingPipeline(result="abc123"),
            policy=SupervisorPolicy(notify_timeout_seconds=0.2),
            events=HangingSink(),  # type: ignore[arg-type]
        )
        await a.scan_once()
        await _wait_end(a)
        assert (await h.row(job.id))["state"] == "succeeded"

    async def test_a_cancel_of_the_job_task_does_not_drop_the_end_card(self) -> None:
        """After the end commits, the lease keeper cancels the work task. The end card must survive."""
        from datetime import datetime, timezone
        from uuid import uuid4

        from personal_agent.dev_agent.jobs import DevJob, JobState

        release = asyncio.Event()
        sent: list[str] = []

        class SlowSink:
            async def notify(self, job_id: UUID, event: str, step: str | None = None) -> None:
                await release.wait()
                sent.append(event)

        job = DevJob(
            id=uuid4(),
            user_id=uuid4(),
            session_id=uuid4(),
            trace_id=uuid4(),
            approval_request_id="a",
            mode="build",
            task_text="t",
            source_artifact_id=None,
            model_id="m",
            state=JobState.SUCCEEDED,
            lease_epoch=1,
            started_at=datetime.now(timezone.utc),
            result_commit=None,
            result_artifact_id=None,
        )
        sup = JobSupervisor(
            store=None,  # type: ignore[arg-type]
            launcher=None,  # type: ignore[arg-type]
            adapter_factory=None,  # type: ignore[arg-type]
            events=SlowSink(),  # type: ignore[arg-type]
        )
        work = asyncio.create_task(sup._notify(job, "end"))
        await asyncio.sleep(0.05)  # the send is in progress
        work.cancel()  # the keeper's cancel of the job task
        with pytest.raises(asyncio.CancelledError):
            await work
        release.set()
        await until(lambda: sent == ["end"])

    async def test_a_resumed_delivery_sends_the_delivering_state(
        self, h: Harness, make_job: MakeJob, pushed: list[tuple[dict[str, object], str]]
    ) -> None:
        job = await make_job()
        first = RecordingPipeline(result="abc123", deliver_gate=asyncio.Event())
        a = h.supervisor("sup-a", behaviour=succeed, pipeline=first)
        await a.scan_once()
        await first.in_delivery.wait()
        await a.stop()

        await expire_lease(h.pool, job.id)
        b = h.supervisor("sup-b", pipeline=RecordingPipeline(result="abc123"), events=self._sink(h))
        await b.scan_once()
        await _wait_end(b)
        assert [(c["event"], c["state"]) for c in self._cards(pushed)] == [
            ("state", "delivering"),
            ("end", "succeeded"),
        ]
