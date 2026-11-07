"""The dev-agent job supervisor: a task in the gateway process (ADR-0159 D9.4–D9.7; FRE-1684).

The supervisor runs at most one job at a time. It takes a job with a lease (:mod:`.jobs`), keeps
the lease with a :class:`LeaseKeeper`, and does every external action through a :class:`Fence`.

A job's path:

1. ``running`` → the pipeline's ``prepare`` (the repo and the workspace clone, FRE-1685) →
   fenced ``issue_token`` → fenced ``start_container`` (sets ``started_at``). The token is issued
   after ``prepare``, so the time of ``prepare`` does not use up the token's grace;
2. the adapter runs the task until it ends, the 30-minute limit passes, or the broker stops the
   token (``cap_reached`` / ``cost_unknown``, D5.8);
3. fenced ``revoke_token`` and ``stop_agent`` (D8.8.1), then the pipeline's handoff;
4. a result → ``checking`` → check → ``delivering`` → delivery → the pipeline's ``advance_main``
   (D8.9) → ``succeeded``;
5. every end state is one fenced ``cleanup``: revoke the token, remove the job's containers and
   volume and verify, then the end state. The job leaves the one-job slot only after that.

**Recovery** (D9.6) runs on every scan, not only at start. A job whose lease expired is taken under
a new lease. A ``delivering`` job resumes its delivery. Any other job is cleaned up and ends
``failed``/``supervisor_restart``. **The orphan sweep** removes containers and volumes of jobs at an
end state, which undoes a late keyed action (D9.5).

**Stop rule.** The keeper renews every 30 s. When no renewal succeeds for 60 s, it cancels the
work. While a fenced action runs, the action holds the job row, so no one can take the lease, and
the keeper waits for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import socket
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Protocol, TypeVar
from uuid import uuid4

import asyncpg  # type: ignore[import-untyped]
import structlog

from personal_agent.delegation.protocols import (
    DelegationContext,
    DelegationExecutorProtocol,
    DelegationOutcome,
    DelegationPackage,
)
from personal_agent.dev_agent.jobs import DevJob, JobState, JobStore, Lease
from personal_agent.dev_agent.launcher import AgentLauncher
from personal_agent.dev_broker.tokens import issue_job_token, revoke_job_token
from personal_agent.exceptions import DevJobRefusedError, LeaseLostError
from personal_agent.telemetry.trace import TraceContext

log = structlog.get_logger(__name__)

T = TypeVar("T")

#: The token stop reasons of D5.8 and the end states they lead to.
_STOP_STATES: dict[str, JobState] = {
    "cap_reached": JobState.CAP_REACHED,
    "cost_unknown": JobState.COST_UNKNOWN,
}


@dataclass(frozen=True)
class SupervisorPolicy:
    """The supervisor's times, in seconds. The defaults are the ADR's numbers (D9.5, D9.7).

    Attributes:
        lease_seconds: Lease length.
        renew_seconds: Time between renewals.
        stop_after_seconds: All work stops after this long with no successful renewal.
        scan_seconds: Time between recovery scans.
        time_limit_seconds: The job limit from the agent container start.
        token_grace_seconds: The job token outlives the limit by this much, as a backstop.
        action_timeout_seconds: The longest time one fenced action can hold the job row.
    """

    lease_seconds: float = 120.0
    renew_seconds: float = 30.0
    stop_after_seconds: float = 60.0
    scan_seconds: float = 30.0
    time_limit_seconds: float = 1800.0
    token_grace_seconds: float = 300.0
    action_timeout_seconds: float = 300.0


class MonotonicClock(Protocol):
    """The supervisor's local time: for its own waits and the stop rule, never for the lease."""

    def monotonic(self) -> float:
        """Return seconds on a clock that never goes back."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait for ``seconds``."""
        ...


class SystemMonotonicClock:
    """The real monotonic clock."""

    def monotonic(self) -> float:
        """Return :func:`time.monotonic`."""
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """Wait for ``seconds`` of real time."""
        await asyncio.sleep(seconds)


class LeaseKeeper:
    """Renews one lease, reads the token stop signal, and enforces the stop rule (D9.5, D5.8)."""

    def __init__(
        self, store: JobStore, lease: Lease, policy: SupervisorPolicy, clock: MonotonicClock
    ) -> None:
        """Create the keeper.

        Args:
            store: The job store.
            lease: The lease to keep.
            policy: The times.
            clock: The local clock.
        """
        self._store = store
        self._lease = lease
        self._policy = policy
        self._clock = clock
        self._last_ok = clock.monotonic()
        self._in_action = 0
        self.lost = False
        self.stop_reason: str | None = None
        self.stopped = asyncio.Event()
        self.renewals = 0

    def renewed(self) -> None:
        """Note a successful renewal (a fence also renews)."""
        self._last_ok = self._clock.monotonic()

    @contextlib.contextmanager
    def action(self) -> Iterator[None]:
        """Mark a fenced action in progress. The keeper does not renew meanwhile."""
        self._in_action += 1
        try:
            yield
        finally:
            self._in_action -= 1
            self.renewed()

    async def run(self, work: asyncio.Task[None]) -> None:
        """Keep the lease until ``work`` ends. Cancel ``work`` when the lease is lost or stale.

        Args:
            work: The job's work task.
        """
        trace = {
            "job_id": str(self._lease.job_id),
            "lease_epoch": self._lease.epoch,
            "trace_id": None,
        }
        # One attempt is short, and a failed attempt is retried soon, so one hung renewal does not
        # use up the whole stop window.
        attempt = self._policy.renew_seconds / 3
        wait = self._policy.renew_seconds
        while not work.done():
            await self._clock.sleep(wait)
            wait = self._policy.renew_seconds
            if work.done():
                return
            if self._in_action:
                # The fenced action holds the row. The lease cannot change hands meanwhile.
                continue
            try:
                async with asyncio.timeout(attempt):
                    signal = await self._store.renew(self._lease, self._policy.lease_seconds)
            except Exception as exc:  # noqa: BLE001 — a failed renewal is retried until the stop rule
                log.warning("dev_job_renew_failed", error_type=type(exc).__name__, **trace)
                wait = attempt
            else:
                if signal is None:
                    self.lost = True
                    log.warning("dev_job_lease_lost", **trace)
                    work.cancel()
                    return
                self.renewals += 1
                self.renewed()
                if signal.status == "stopped" and signal.stop_reason and not self.stopped.is_set():
                    self.stop_reason = signal.stop_reason
                    self.stopped.set()
                    log.info("dev_job_token_stopped", stop_reason=signal.stop_reason, **trace)
            # A renewal that waited on the row of a fenced action is not a stale lease.
            stale = self._clock.monotonic() - self._last_ok >= self._policy.stop_after_seconds
            if stale and not self._in_action:
                self.lost = True
                log.error(
                    "dev_job_renewal_stale_work_stopped",
                    stop_after_seconds=self._policy.stop_after_seconds,
                    **trace,
                )
                work.cancel()
                return


class Fence:
    """The only way for the supervisor and the pipeline to do an external action (D9.5)."""

    def __init__(
        self, store: JobStore, lease: Lease, policy: SupervisorPolicy, keeper: LeaseKeeper | None
    ) -> None:
        """Create the fence of one lease.

        Args:
            store: The job store.
            lease: The lease that every action requires.
            policy: The times.
            keeper: The lease keeper, told about each fenced renewal.
        """
        self._store = store
        self.lease = lease
        self._policy = policy
        self._keeper = keeper

    async def run(
        self,
        action: str,
        operation: Callable[[asyncpg.Connection], Awaitable[T]],
        *,
        mark_started: bool = False,
    ) -> T:
        """Run one action under the lease. See :meth:`JobStore.fenced`.

        Args:
            action: The action name for the log.
            operation: The action. It receives the fenced transaction's connection.
            mark_started: Set ``started_at`` (the container start).

        Returns:
            What ``operation`` returned.

        Raises:
            LeaseLostError: If the lease is not held. The action did not run.
        """
        timeout = self._policy.action_timeout_seconds

        async def bounded(conn: asyncpg.Connection) -> T:
            async with asyncio.timeout(timeout):
                return await operation(conn)

        marker = self._keeper.action() if self._keeper is not None else contextlib.nullcontext()
        with marker:
            return await self._store.fenced(
                self.lease,
                action,
                bounded,
                lease_seconds=self._policy.lease_seconds,
                mark_started=mark_started,
            )


@dataclass(frozen=True)
class PreparedJob:
    """What ``prepare`` gives the agent container.

    Attributes:
        job: The job, with ``repo`` and ``base_commit`` set when the pipeline has a repo.
        workspace: A tar of the clone for the workspace volume, or ``None`` for an empty one.
    """

    job: DevJob
    workspace: bytes | None


class JobResultPipeline(Protocol):
    """The repo steps around the agent (FRE-1685), and the check and delivery (FRE-1686).

    Every external action of a step goes through ``fence.run``. A step that raises
    :class:`DevJobRefusedError` ends the job with that reason.
    """

    async def prepare(self, job: DevJob, fence: Fence) -> PreparedJob:
        """Make the repo and the workspace clone, before the agent container starts (D8.4)."""
        ...

    async def handoff(self, job: DevJob, fence: Fence) -> str | None:
        """Take the agent's commits out of the workspace. Return the result commit, or ``None``."""
        ...

    async def check(self, job: DevJob, result_commit: str, fence: Fence) -> bool:
        """Run the publish check. Return whether it passed."""
        ...

    async def deliver(self, job: DevJob, fence: Fence) -> None:
        """Publish the result. Idempotent (D9.8): a retry after a crash publishes once."""
        ...

    async def advance_main(self, job: DevJob, fence: Fence) -> None:
        """After the publish, fast-forward the repo's ``main`` to the result, if it can (D8.9)."""
        ...


class NoResultPipeline:
    """A pipeline with no repo: an empty workspace and no handoff, so no job has a result."""

    async def prepare(self, job: DevJob, fence: Fence) -> PreparedJob:
        """Return the job unchanged, with an empty workspace."""
        del fence
        return PreparedJob(job=job, workspace=None)

    async def handoff(self, job: DevJob, fence: Fence) -> str | None:
        """Return ``None``: the handoff is not built yet."""
        del job, fence
        return None

    async def check(self, job: DevJob, result_commit: str, fence: Fence) -> bool:
        """Never reached without a handoff."""
        raise NotImplementedError("the publish check arrives with FRE-1686")

    async def deliver(self, job: DevJob, fence: Fence) -> None:
        """Never reached without a check."""
        raise NotImplementedError("delivery arrives with FRE-1686")

    async def advance_main(self, job: DevJob, fence: Fence) -> None:
        """Never reached without a delivery."""
        del job, fence


class _EndFailedError(Exception):
    """An end state could not be written. It carries the intended end, so a retry keeps it."""

    def __init__(self, state: JobState, reason: str | None, *, revoked: bool) -> None:
        super().__init__(f"could not end the job as {state.value}")
        self.state = state
        self.reason = reason
        self.revoked = revoked


#: Opens the job's adapter for the run and closes it after. Arguments: the job, the server password.
AdapterFactory = Callable[[DevJob, str], AbstractAsyncContextManager[DelegationExecutorProtocol]]


def supervisor_owner_id() -> str:
    """Return an owner id unique to this process start."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"


class JobSupervisor:
    """Takes jobs, runs them one at a time, and recovers jobs whose lease expired."""

    def __init__(
        self,
        *,
        store: JobStore,
        launcher: AgentLauncher,
        adapter_factory: AdapterFactory,
        pipeline: JobResultPipeline | None = None,
        policy: SupervisorPolicy | None = None,
        clock: MonotonicClock | None = None,
        owner: str | None = None,
    ) -> None:
        """Create the supervisor.

        Args:
            store: The job store.
            launcher: Starts and removes the job's containers.
            adapter_factory: Opens the job's adapter.
            pipeline: The steps after the agent. Defaults to :class:`NoResultPipeline`.
            policy: The times. Defaults to the ADR's numbers.
            clock: The local clock. Defaults to the system's monotonic clock.
            owner: The lease owner id. Defaults to :func:`supervisor_owner_id`.
        """
        self._store = store
        self._launcher = launcher
        self._adapter_factory = adapter_factory
        self._pipeline = pipeline or NoResultPipeline()
        self.policy = policy or SupervisorPolicy()
        self._clock = clock or SystemMonotonicClock()
        self.owner = owner or supervisor_owner_id()
        self._current: asyncio.Task[None] | None = None
        self._current_job: DevJob | None = None
        self._current_fence: Fence | None = None
        self.keeper: LeaseKeeper | None = None

    @property
    def busy(self) -> bool:
        """Whether this supervisor runs a job now."""
        return self._current is not None and not self._current.done()

    async def run_forever(self) -> None:
        """Scan on every interval until cancelled. Cancellation stops the job without cleanup."""
        log.info("dev_job_supervisor_started", owner=self.owner, trace_id=None)
        try:
            while True:
                try:
                    await self.scan_once()
                except Exception as exc:  # noqa: BLE001 — a failed scan retries at the next interval
                    log.error(
                        "dev_job_scan_failed",
                        owner=self.owner,
                        error_type=type(exc).__name__,
                        error=str(exc),
                        trace_id=None,
                    )
                await self._clock.sleep(self.policy.scan_seconds)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop the current job's work, then revoke its token as a best effort.

        The revoke stops the agent's model calls at once (the job would not resume anyway, D9.6).
        Recovery removes the containers and writes the end state after the lease expires.
        """
        if self._current is None or self._current.done():
            return
        job, fence = self._current_job, self._current_fence
        self._current.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._current
        if job is None or fence is None:
            return

        async def revoke(conn: asyncpg.Connection) -> None:
            await revoke_job_token(conn, job.id)

        try:
            async with asyncio.timeout(10):
                await fence.run("revoke_token", revoke)
        except Exception as exc:  # noqa: BLE001 — best effort; recovery revokes it later
            log.warning(
                "dev_job_stop_revoke_failed",
                job_id=str(job.id),
                error_type=type(exc).__name__,
                trace_id=job.trace_id.hex,
            )

    async def scan_once(self) -> None:
        """Recover expired jobs, sweep orphans, and take a new job if this supervisor is idle."""
        await self.recover()
        await self.sweep_orphans()
        if self.busy:
            return
        taken = await self._store.claim_new(self.owner, self.policy.lease_seconds)
        if taken is not None:
            job, lease = taken
            self._start(job, lease, self._work)

    async def recover(self) -> None:
        """Take every job whose lease expired, and resume or clean it up (D9.6)."""
        while (
            taken := await self._store.take_expired(self.owner, self.policy.lease_seconds)
        ) is not None:
            job, lease = taken
            log.warning(
                "dev_job_recovered",
                job_id=str(job.id),
                state=job.state.value,
                lease_epoch=lease.epoch,
                trace_id=job.trace_id.hex,
            )
            if self._current_job is not None and self._current_job.id == job.id:
                # This process's own run lost the lease. Stop it before acting on the job.
                await self.stop()
            if job.state is JobState.DELIVERING:
                self._start(job, lease, self._resume_delivery)
                continue
            try:
                await self._end(
                    job,
                    Fence(self._store, lease, self.policy, None),
                    JobState.FAILED,
                    "supervisor_restart",
                )
            except Exception as exc:  # noqa: BLE001 — the job stays active; its lease expires; retried
                log.error(
                    "dev_job_recovery_cleanup_failed",
                    job_id=str(job.id),
                    error_type=type(exc).__name__,
                    error=str(exc),
                    trace_id=job.trace_id.hex,
                )

    async def sweep_orphans(self) -> None:
        """Remove containers and volumes of jobs that are at an end state or have no row (D9.5)."""
        labelled = await self._launcher.labelled_job_ids()
        for job_id in await self._store.ended_or_missing(labelled):
            try:
                await self._launcher.remove(job_id)
                log.warning("dev_job_orphan_removed", job_id=str(job_id), trace_id=None)
            except Exception as exc:  # noqa: BLE001 — retried on the next sweep
                log.error(
                    "dev_job_orphan_remove_failed",
                    job_id=str(job_id),
                    error_type=type(exc).__name__,
                    trace_id=None,
                )

    def _start(
        self,
        job: DevJob,
        lease: Lease,
        body: Callable[[DevJob, Fence, LeaseKeeper], Awaitable[None]],
    ) -> None:
        keeper = LeaseKeeper(self._store, lease, self.policy, self._clock)
        fence = Fence(self._store, lease, self.policy, keeper)
        self.keeper = keeper
        self._current_job = job
        self._current_fence = fence

        async def guarded() -> None:
            try:
                await body(job, fence, keeper)
            except LeaseLostError:
                log.warning(
                    "dev_job_work_stopped_lease_lost", job_id=str(job.id), trace_id=job.trace_id.hex
                )
            except DevJobRefusedError as refused:
                # Any pipeline step's refusal keeps its reason (the JobResultPipeline contract).
                log.warning(
                    "dev_job_refused",
                    job_id=str(job.id),
                    reason=refused.reason,
                    trace_id=job.trace_id.hex,
                )
                with contextlib.suppress(Exception):
                    await self._end(job, fence, JobState.FAILED, refused.reason)
            except _EndFailedError as failed:
                # Retry the same end once. A second failure leaves the job to recovery.
                log.error(
                    "dev_job_end_failed",
                    job_id=str(job.id),
                    state=failed.state.value,
                    error_type=type(failed.__cause__).__name__,
                    trace_id=job.trace_id.hex,
                )
                with contextlib.suppress(Exception):
                    await self._end(job, fence, failed.state, failed.reason, revoked=failed.revoked)
            except Exception as exc:  # noqa: BLE001 — end the job; a failed end is recovered later
                log.error(
                    "dev_job_work_failed",
                    job_id=str(job.id),
                    error_type=type(exc).__name__,
                    error=str(exc),
                    trace_id=job.trace_id.hex,
                )
                with contextlib.suppress(Exception):
                    await self._end(
                        job, fence, JobState.FAILED, f"supervisor_error: {type(exc).__name__}"
                    )

        async def with_keeper() -> None:
            work = asyncio.create_task(guarded())
            keeping = asyncio.create_task(keeper.run(work))
            try:
                with contextlib.suppress(asyncio.CancelledError):
                    await work
            finally:
                keeping.cancel()
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await keeping
                with contextlib.suppress(asyncio.CancelledError):
                    await work

        self._current = asyncio.create_task(with_keeper())

    async def _work(self, job: DevJob, fence: Fence, keeper: LeaseKeeper) -> None:
        policy = self.policy
        password = secrets.token_urlsafe(32)

        async def issue(conn: asyncpg.Connection) -> str:
            expires = await conn.fetchval(
                "SELECT clock_timestamp() + make_interval(secs => $1)",
                policy.time_limit_seconds + policy.token_grace_seconds,
            )
            return await issue_job_token(
                conn,
                job_id=job.id,
                model_key=job.model_id,
                trace_id=job.trace_id,
                session_id=job.session_id,
                expires_at=expires,
            )

        await self._store.transition(fence.lease, JobState.RUNNING)
        try:
            prepared = await self._pipeline.prepare(job, fence)
        except DevJobRefusedError as refused:
            log.warning(
                "dev_job_refused",
                job_id=str(job.id),
                reason=refused.reason,
                trace_id=job.trace_id.hex,
            )
            await self._end(job, fence, JobState.FAILED, refused.reason)
            return
        job = prepared.job
        token = await fence.run("issue_token", issue)

        async def start(conn: asyncpg.Connection) -> None:
            del conn
            await self._launcher.start(
                job.id, job_token=token, server_password=password, workspace=prepared.workspace
            )

        await fence.run("start_container", start, mark_started=True)
        log.info("dev_job_running", job_id=str(job.id), trace_id=job.trace_id.hex)

        left = await self._store.seconds_left(job.id, policy.time_limit_seconds)
        outcome = await self._run_agent(job, password, max(left, 0.0), keeper)
        # Measured before the revoke and the container stop, which take seconds of their own.
        timed_out = await self._store.seconds_left(job.id, policy.time_limit_seconds) <= 0

        async def read_and_revoke(conn: asyncpg.Connection) -> str | None:
            # A stop that came after the last renewal is read here, before the revoke overwrites it.
            row = await conn.fetchrow(
                "SELECT status, stop_reason FROM dev_job_tokens WHERE job_id = $1", job.id
            )
            await revoke_job_token(conn, job.id)
            if row is not None and row["status"] == "stopped":
                return str(row["stop_reason"]) if row["stop_reason"] else None
            return None

        stop_reason = await fence.run("revoke_token", read_and_revoke) or keeper.stop_reason
        if stop_reason is not None:
            state = _STOP_STATES.get(stop_reason, JobState.FAILED)
            await self._end(job, fence, state, stop_reason, revoked=True)
            return

        async def stop_agent(conn: asyncpg.Connection) -> None:
            del conn
            await self._launcher.stop(job.id)

        await fence.run("stop_agent", stop_agent)
        try:
            result = await self._pipeline.handoff(job, fence)
        except DevJobRefusedError as refused:
            log.warning(
                "dev_job_result_refused",
                job_id=str(job.id),
                reason=refused.reason,
                trace_id=job.trace_id.hex,
            )
            state = JobState.TIMED_OUT if timed_out else JobState.FAILED
            await self._end(job, fence, state, refused.reason, revoked=True)
            return
        if result is None:
            if timed_out:
                await self._end(job, fence, JobState.TIMED_OUT, "time_limit", revoked=True)
            elif outcome is not None and outcome.success:
                await self._end(job, fence, JobState.FAILED, "no_result", revoked=True)
            else:
                missing = outcome.what_was_missing if outcome is not None else "no_outcome"
                await self._end(job, fence, JobState.FAILED, f"agent: {missing}", revoked=True)
            return
        job = replace(job, result_commit=result)
        await self._store.transition(fence.lease, JobState.CHECKING)
        if not await self._pipeline.check(job, result, fence):
            await self._end(job, fence, JobState.FAILED_CHECK, "check_failed", revoked=True)
            return
        await self._store.transition(fence.lease, JobState.DELIVERING)
        await self._deliver(job, fence)

    async def _resume_delivery(self, job: DevJob, fence: Fence, keeper: LeaseKeeper) -> None:
        del keeper
        await self._deliver(job, fence)

    async def _deliver(self, job: DevJob, fence: Fence) -> None:
        try:
            await self._pipeline.deliver(job, fence)
        except LeaseLostError:
            raise
        except Exception as exc:  # noqa: BLE001 — a delivery error ends the job; a crash resumes it
            log.error(
                "dev_job_delivery_failed",
                job_id=str(job.id),
                error_type=type(exc).__name__,
                trace_id=job.trace_id.hex,
            )
            await self._end(
                job, fence, JobState.FAILED, f"delivery_failed: {type(exc).__name__}", revoked=True
            )
            return
        await self._advance_main(job, fence)
        await self._end(job, fence, JobState.SUCCEEDED, None, revoked=True)

    async def _advance_main(self, job: DevJob, fence: Fence) -> None:
        """Try ``advance_main`` twice. The page is published, so a failure never fails the job.

        After two failures ``main_moved`` stays ``NULL``: the outcome is unknown (D8.9).
        """
        for attempt in (1, 2):
            try:
                await self._pipeline.advance_main(job, fence)
                return
            except LeaseLostError:
                raise
            except Exception as exc:  # noqa: BLE001 — the page is published; main is secondary
                log.warning(
                    "dev_job_advance_main_failed",
                    job_id=str(job.id),
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    trace_id=job.trace_id.hex,
                )
        log.error("dev_job_main_moved_unknown", job_id=str(job.id), trace_id=job.trace_id.hex)

    async def _run_agent(
        self, job: DevJob, password: str, timeout: float, keeper: LeaseKeeper
    ) -> DelegationOutcome | None:
        package = DelegationPackage(
            task_id=str(job.id),
            target_agent="opencode",
            task_description=job.task_text,
            context=DelegationContext(service_path="/workspace"),
            created_at=datetime.now(timezone.utc),
        )
        trace_ctx = TraceContext(trace_id=job.trace_id.hex)
        async with self._adapter_factory(job, password) as adapter:
            run = asyncio.create_task(adapter.delegate(package, timeout, trace_ctx))
            stopped = asyncio.create_task(keeper.stopped.wait())
            try:
                await asyncio.wait({run, stopped}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (run, stopped):
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
            return run.result() if run.done() and not run.cancelled() else None

    async def _end(
        self,
        job: DevJob,
        fence: Fence,
        state: JobState,
        reason: str | None,
        *,
        revoked: bool = False,
    ) -> None:
        """Revoke the token, then remove and write the end state. The slot frees only after both.

        The revoke is its own fence, so it commits even when the removal then fails.

        Args:
            job: The job.
            fence: The fence of the current lease.
            state: The end state.
            reason: The failure reason, or ``None``.
            revoked: The token was revoked under this lease already, so skip the revoke.

        Raises:
            LeaseLostError: If the lease is not held.
            _EndFailedError: If the revoke or the cleanup failed. It carries the intended end.
        """

        async def revoke(conn: asyncpg.Connection) -> None:
            await revoke_job_token(conn, job.id)

        async def cleanup(conn: asyncpg.Connection) -> None:
            await self._launcher.remove(job.id)
            await JobStore.finish(conn, fence.lease, state, reason)

        try:
            if not revoked:
                await fence.run("revoke_token", revoke)
            await fence.run("cleanup", cleanup)
        except LeaseLostError:
            raise
        except Exception as exc:
            raise _EndFailedError(state, reason, revoked=revoked) from exc
        log.info(
            "dev_job_ended",
            job_id=str(job.id),
            state=state.value,
            failure_reason=reason,
            trace_id=job.trace_id.hex,
        )
