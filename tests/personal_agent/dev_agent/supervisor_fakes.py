"""Fakes for the supervisor tests (FRE-1684, FRE-1685): a clock, a launcher, an adapter, a pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from personal_agent.delegation.protocols import (
    DelegationExecutorProtocol,
    DelegationOutcome,
    DelegationPackage,
)
from personal_agent.dev_agent.jobs import DevJob, JobStore, Lease, TokenSignal
from personal_agent.dev_agent.supervisor import Fence, PreparedJob
from personal_agent.exceptions import DevAgentLaunchError, DevJobRefusedError
from personal_agent.telemetry.trace import TraceContext


class ManualClock:
    """A monotonic clock that moves only when the test calls :meth:`advance`."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((self.now + seconds, future))
        await future

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        for due, future in list(self._waiters):
            if due <= self.now and not future.done():
                future.set_result(None)
                self._waiters.remove((due, future))
        for _ in range(20):
            await asyncio.sleep(0)


class FakeLauncher:
    """Records every call in order. ``containers`` is what Docker would list."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, UUID, str]] = []
        self.containers: set[UUID] = set()
        self.start_gate: asyncio.Event | None = None
        self.inside_start = asyncio.Event()
        self.fail_remove = 0
        self.who = "?"
        self.workspaces: dict[UUID, bytes | None] = {}

    async def start(
        self,
        job_id: UUID,
        *,
        job_token: str,
        server_password: str,
        workspace: bytes | None = None,
    ) -> None:
        assert job_token and server_password
        self.workspaces[job_id] = workspace
        self.inside_start.set()
        if self.start_gate is not None:
            await self.start_gate.wait()
        self.calls.append(("start", job_id, self.who))
        self.containers.add(job_id)

    async def stop(self, job_id: UUID) -> None:
        self.calls.append(("stop", job_id, self.who))

    async def remove(self, job_id: UUID) -> None:
        if self.fail_remove:
            self.fail_remove -= 1
            self.calls.append(("remove_failed", job_id, self.who))
            raise DevAgentLaunchError("seeded remove failure")
        self.calls.append(("remove", job_id, self.who))
        self.containers.discard(job_id)

    async def labelled_job_ids(self) -> set[UUID]:
        return set(self.containers)

    async def bundle_workspace(self, job_id: UUID, branch: str) -> bytes:
        self.calls.append(("bundle", job_id, self.who))
        return b""

    def ops(self, job_id: UUID) -> list[str]:
        return [op for op, jid, _ in self.calls if jid == job_id]


Behaviour = Callable[[DelegationPackage, float], Awaitable[DelegationOutcome]]


async def block_forever(package: DelegationPackage, timeout: float) -> DelegationOutcome:
    """An agent that never ends."""
    del timeout
    await asyncio.Event().wait()
    raise AssertionError(package.task_id)


async def succeed(package: DelegationPackage, timeout: float) -> DelegationOutcome:
    """An agent that ends at once with success."""
    del timeout
    return DelegationOutcome(package.task_id, True, 1, "done", "")


class FakeAdapter:
    def __init__(self, behaviour: Behaviour, record: list[float]) -> None:
        self._behaviour = behaviour
        self._record = record

    def available(self) -> bool:
        return True

    async def delegate(
        self, package: DelegationPackage, timeout: float, trace_ctx: TraceContext
    ) -> DelegationOutcome:
        del trace_ctx
        self._record.append(timeout)
        return await self._behaviour(package, timeout)


def adapter_factory(
    behaviour: Behaviour, record: list[float]
) -> Callable[[DevJob, str], AsyncIterator[DelegationExecutorProtocol]]:
    @asynccontextmanager
    async def factory(job: DevJob, password: str) -> AsyncIterator[DelegationExecutorProtocol]:
        assert password
        del job
        yield FakeAdapter(behaviour, record)

    return factory  # type: ignore[return-value]


class RecordingPipeline:
    """A pipeline whose result, check and delivery the test chooses. Delivery is fenced."""

    def __init__(
        self,
        result: str | None = None,
        passes: bool = True,
        deliver_gate: asyncio.Event | None = None,
        refuse_prepare: str | None = None,
        refuse_handoff: str | None = None,
        advance_failures: int = 0,
    ) -> None:
        self.result = result
        self.passes = passes
        self.deliver_gate = deliver_gate
        self.refuse_prepare = refuse_prepare
        self.refuse_handoff = refuse_handoff
        self.advance_failures = advance_failures
        self.in_delivery = asyncio.Event()
        self.calls: list[str] = []
        self.seen_results: list[str | None] = []

    async def prepare(self, job: DevJob, fence: Fence) -> PreparedJob:
        del fence
        self.calls.append("prepare")
        if self.refuse_prepare is not None:
            raise DevJobRefusedError(self.refuse_prepare)
        return PreparedJob(job=job, workspace=b"workspace-tar")

    async def handoff(self, job: DevJob, fence: Fence) -> str | None:
        del job, fence
        self.calls.append("handoff")
        if self.refuse_handoff is not None:
            raise DevJobRefusedError(self.refuse_handoff)
        return self.result

    async def check(self, job: DevJob, result_commit: str, fence: Fence) -> bool:
        del job, fence
        self.calls.append(f"check {result_commit}")
        return self.passes

    async def deliver(self, job: DevJob, fence: Fence) -> None:
        del job
        self.calls.append("deliver")
        self.in_delivery.set()
        if self.deliver_gate is not None:
            await self.deliver_gate.wait()

        async def upload(conn: asyncpg.Connection) -> None:
            del conn
            self.calls.append("upload")

        await fence.run("upload", upload)

    async def advance_main(self, job: DevJob, fence: Fence) -> None:
        del fence
        self.calls.append("advance_main")
        self.seen_results.append(job.result_commit)
        if self.advance_failures:
            self.advance_failures -= 1
            raise ConnectionError("seeded: GitHub unreachable")


class PausingStore(JobStore):
    """Pauses a supervisor just before the fence of one action (codex finding 6)."""

    def __init__(self, pool: asyncpg.Pool, pause_before: str) -> None:
        super().__init__(pool)
        self.pause_before = pause_before
        self.reached = asyncio.Event()
        self.gate = asyncio.Event()

    async def fenced(self, lease: Lease, action: str, operation, **kwargs):  # type: ignore[no-untyped-def]
        if action == self.pause_before:
            self.reached.set()
            await self.gate.wait()
        return await super().fenced(lease, action, operation, **kwargs)


class FailingRenewStore(JobStore):
    """A store whose renewals always fail, as if the database were unreachable."""

    async def renew(self, lease: Lease, lease_seconds: float) -> TokenSignal | None:
        raise ConnectionError("seeded: database unreachable")


async def until(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    """Wait until ``condition()`` is true."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)
