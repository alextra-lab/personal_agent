"""A job supervisor in its own process, for the live restart and pause tests (FRE-1684).

``test_supervisor_live.py`` starts it with the test stack's environment, then kills it with SIGKILL
(a gateway restart) or lets it stop itself with SIGSTOP before a fence (a paused supervisor). The
agent never ends, so the job stays ``running`` until the parent acts.

Usage: ``python live_child.py <scope> <network> <toolkit> <image> <owner> [pause_before]``
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import asyncpg  # type: ignore[import-untyped]
from toolkit_fakes import synthetic_assets  # the script directory is on sys.path

from personal_agent.config import settings
from personal_agent.delegation.protocols import (
    DelegationExecutorProtocol,
    DelegationOutcome,
    DelegationPackage,
)
from personal_agent.dev_agent.container_spec import load_profiles
from personal_agent.dev_agent.jobs import DevJob, JobStore, Lease
from personal_agent.dev_agent.launcher import DockerLauncher
from personal_agent.dev_agent.supervisor import JobSupervisor, SupervisorPolicy
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn
from personal_agent.telemetry.trace import TraceContext

#: Short times, so the parent need not wait two minutes for an expiry. The protocol is the same.
LIVE_POLICY = SupervisorPolicy(
    lease_seconds=6.0, renew_seconds=2.0, stop_after_seconds=4.0, scan_seconds=1.0
)


class _NeverEnds:
    def available(self) -> bool:
        return True

    async def delegate(
        self, package: DelegationPackage, timeout: float, trace_ctx: TraceContext
    ) -> DelegationOutcome:
        del timeout, trace_ctx
        await asyncio.Event().wait()
        return DelegationOutcome(package.task_id, False, 0, "", "unreachable")


@asynccontextmanager
async def _factory(job: DevJob, password: str) -> AsyncIterator[DelegationExecutorProtocol]:
    del job, password
    yield _NeverEnds()


class _SelfPausingStore(JobStore):
    """Stops its own process with SIGSTOP just before the fence of one action."""

    def __init__(self, pool: asyncpg.Pool, pause_before: str | None) -> None:
        super().__init__(pool)
        self._pause_before = pause_before

    async def fenced(
        self,
        lease: Lease,
        action: str,
        operation: Callable[[asyncpg.Connection], Awaitable[object]],
        **kwargs: object,
    ) -> object:
        if action == self._pause_before:
            self._pause_before = None
            os.kill(os.getpid(), signal.SIGSTOP)
        return await super().fenced(lease, action, operation, **kwargs)  # type: ignore[arg-type]


async def main(argv: list[str]) -> None:
    scope, network, toolkit, image, owner = argv[:5]
    pause_before = argv[5] if len(argv) > 5 else None
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.database_url), min_size=1, max_size=4
    )
    profile = load_profiles().agent.model_copy(update={"image": image})
    supervisor = JobSupervisor(
        store=_SelfPausingStore(pool, pause_before),
        launcher=DockerLauncher(
            profile,
            network_name=network,
            toolkit_volume=toolkit,
            toolkit_assets=synthetic_assets(),
            scope=scope,
        ),
        adapter_factory=_factory,  # type: ignore[arg-type]
        policy=LIVE_POLICY,
        owner=owner,
    )
    await supervisor.run_forever()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
