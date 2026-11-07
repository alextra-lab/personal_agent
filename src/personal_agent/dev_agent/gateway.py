"""The production wiring of the dev-agent job supervisor in the gateway (ADR-0159 D9.4; FRE-1684).

The gateway lifespan calls :func:`start_supervisor` when ``dev_agent_supervisor_enabled`` is set,
and :func:`stop_supervisor` at shutdown. A stopped supervisor leaves its job as it is: the next
gateway's recovery cleans it up after the lease expires (D9.6).
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg  # type: ignore[import-untyped]
import structlog

from personal_agent.config import settings
from personal_agent.delegation.adapters.opencode import OpenCodeAdapter, opencode_client
from personal_agent.delegation.protocols import DelegationExecutorProtocol
from personal_agent.dev_agent.container_spec import load_profiles
from personal_agent.dev_agent.jobs import DevJob, JobStore
from personal_agent.dev_agent.launcher import DockerLauncher
from personal_agent.dev_agent.supervisor import AdapterFactory, JobSupervisor, SupervisorPolicy
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RunningSupervisor:
    """The supervisor task and the pool it owns.

    Attributes:
        supervisor: The supervisor.
        task: Its ``run_forever`` task.
        pool: Its database pool.
    """

    supervisor: JobSupervisor
    task: asyncio.Task[None]
    pool: asyncpg.Pool


def relay_adapter_factory(control_url: str, control_secret: str) -> AdapterFactory:
    """Return the factory that opens a job's :class:`OpenCodeAdapter` through the relay.

    Args:
        control_url: The broker's control API.
        control_secret: The relay's secret.

    Returns:
        A function of the job and its server password that yields the adapter and closes its
        client after the run.
    """

    @asynccontextmanager
    async def factory(job: DevJob, password: str) -> AsyncIterator[DelegationExecutorProtocol]:
        client = opencode_client(
            control_url=control_url,
            job_id=job.id,
            control_secret=control_secret,
            server_password=password,
        )

        async def progress(step: str) -> None:
            log.info("dev_job_progress", job_id=str(job.id), step=step, trace_id=job.trace_id.hex)

        try:
            yield OpenCodeAdapter(client, model_id=job.model_id, on_progress=progress)
        finally:
            await client.aclose()

    return factory


async def start_supervisor() -> RunningSupervisor | None:
    """Build the supervisor from settings and start it.

    Returns:
        The running supervisor, or ``None`` when the control secret is not set.
    """
    secret = settings.dev_broker_control_secret
    if not secret:
        log.error(
            "dev_job_supervisor_not_started",
            reason="AGENT_DEV_BROKER_CONTROL_SECRET is not set",
            trace_id=None,
        )
        return None
    profiles = load_profiles()
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.database_url), min_size=1, max_size=4, command_timeout=30
    )
    supervisor = JobSupervisor(
        store=JobStore(pool),
        launcher=DockerLauncher(
            profiles.agent,
            network_name=settings.dev_agent_network,
            toolkit_volume=settings.dev_agent_toolkit_volume,
            scope=settings.dev_agent_label_scope,
        ),
        adapter_factory=relay_adapter_factory(settings.dev_broker_control_url, secret),
        policy=SupervisorPolicy(time_limit_seconds=float(profiles.agent.wall_clock_seconds)),
    )
    # A fresh context: the task outlives the startup span (ADR-0129 D3, FRE-1069).
    task = asyncio.create_task(supervisor.run_forever(), context=contextvars.Context())
    return RunningSupervisor(supervisor=supervisor, task=task, pool=pool)


async def stop_supervisor(running: RunningSupervisor) -> None:
    """Stop the supervisor task and close its pool.

    Args:
        running: What :func:`start_supervisor` returned.
    """
    running.task.cancel()
    try:
        await running.task
    except asyncio.CancelledError:
        pass
    await running.pool.close()
    log.info("dev_job_supervisor_stopped", owner=running.supervisor.owner, trace_id=None)
