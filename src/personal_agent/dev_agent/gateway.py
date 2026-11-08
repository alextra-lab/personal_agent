"""The production wiring of the dev-agent job supervisor in the gateway (ADR-0159 D9.4; FRE-1684).

The gateway lifespan calls :func:`start_supervisor` when ``dev_agent_supervisor_enabled`` is set,
and :func:`stop_supervisor` at shutdown. The supervisor needs the broker's control secret and the
GitHub App (FRE-1685). Without either, it does not start. A stopped supervisor leaves its job as it is: the next
gateway's recovery cleans it up after the lease expires (D9.6).
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg  # type: ignore[import-untyped]
import httpx
import structlog

from personal_agent.config import settings
from personal_agent.delegation.adapters.opencode import OpenCodeAdapter, opencode_client
from personal_agent.delegation.protocols import DelegationExecutorProtocol
from personal_agent.dev_agent.container_spec import load_profiles
from personal_agent.dev_agent.github_app import GitHubAppConfig, GitHubAppHost
from personal_agent.dev_agent.job_card import JobEventSink, PoolJobEventSink
from personal_agent.dev_agent.jobs import DevJob, JobStore
from personal_agent.dev_agent.launcher import DockerLauncher
from personal_agent.dev_agent.repo_pipeline import GitHubRepoPipeline
from personal_agent.dev_agent.supervisor import AdapterFactory, JobSupervisor, SupervisorPolicy
from personal_agent.dev_agent.toolkit import load_toolkit_assets
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn
from personal_agent.security import create_guarded_http_client
from personal_agent.storage.artifact_store import get_artifact_store
from personal_agent.transport.agui.transport import emit_dev_job

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RunningSupervisor:
    """The supervisor task and what it owns.

    Attributes:
        supervisor: The supervisor.
        task: Its ``run_forever`` task.
        pool: Its database pool.
        http: Its GitHub API client.
    """

    supervisor: JobSupervisor
    task: asyncio.Task[None]
    pool: asyncpg.Pool
    http: httpx.AsyncClient


def relay_adapter_factory(
    control_url: str, control_secret: str, events: JobEventSink | None = None
) -> AdapterFactory:
    """Return the factory that opens a job's :class:`OpenCodeAdapter` through the relay.

    Args:
        control_url: The broker's control API.
        control_secret: The relay's secret.
        events: Receives each step of the OpenCode event stream for the job card (D10.6).

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
            if events is not None:
                await events.notify(job.id, "step", step)

        try:
            yield OpenCodeAdapter(client, model_id=job.model_id, on_progress=progress)
        finally:
            await client.aclose()

    return factory


def github_config() -> GitHubAppConfig | None:
    """Return the GitHub App config from settings, or ``None`` when a value is missing.

    Returns:
        The config, or ``None``.

    Raises:
        ValueError: If the private key value does not decode to a PEM key.
    """
    app_id = settings.dev_agent_github_app_id
    installation_id = settings.dev_agent_github_installation_id
    org = settings.dev_agent_github_org
    key = settings.dev_agent_github_app_private_key_b64
    if app_id is None or installation_id is None or not org or not key:
        return None
    return GitHubAppConfig.from_base64_key(
        app_id=app_id,
        installation_id=installation_id,
        org=org,
        private_key_b64=key,
        api_url=settings.dev_agent_github_api_url,
    )


async def start_supervisor() -> RunningSupervisor | None:
    """Build the supervisor from settings and start it.

    Returns:
        The running supervisor, or ``None`` when the control secret or the GitHub App is not set.
    """
    secret = settings.dev_broker_control_secret
    if not secret:
        log.error(
            "dev_job_supervisor_not_started",
            reason="AGENT_DEV_BROKER_CONTROL_SECRET is not set",
            trace_id=None,
        )
        return None
    github = github_config()
    if github is None:
        log.error(
            "dev_job_supervisor_not_started",
            reason="the AGENT_DEV_AGENT_GITHUB_* settings are not all set",
            trace_id=None,
        )
        return None
    profiles = load_profiles()
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.database_url), min_size=1, max_size=4, command_timeout=30
    )
    # ADR-0132 D2: DomainGuard checks every GitHub URL before a connection is formed.
    http = create_guarded_http_client()
    launcher = DockerLauncher(
        profiles.agent,
        network_name=settings.dev_agent_network,
        toolkit_volume=settings.dev_agent_toolkit_volume,
        toolkit_assets=load_toolkit_assets(),
        scope=settings.dev_agent_label_scope,
        handoff_profile=profiles.handoff,
        check_profile=profiles.check,
    )
    events = PoolJobEventSink(pool, emit_dev_job)
    supervisor = JobSupervisor(
        store=JobStore(pool),
        launcher=launcher,
        adapter_factory=relay_adapter_factory(settings.dev_broker_control_url, secret, events),
        pipeline=GitHubRepoPipeline(
            pool=pool,
            launcher=launcher,
            host=GitHubAppHost(github, http),
            pages=get_artifact_store(),
            checker=launcher,
        ),
        policy=SupervisorPolicy(time_limit_seconds=float(profiles.agent.wall_clock_seconds)),
        events=events,
    )
    # A fresh context: the task outlives the startup span (ADR-0129 D3, FRE-1069).
    task = asyncio.create_task(supervisor.run_forever(), context=contextvars.Context())
    return RunningSupervisor(supervisor=supervisor, task=task, pool=pool, http=http)


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
    await running.http.aclose()
    log.info("dev_job_supervisor_stopped", owner=running.supervisor.owner, trace_id=None)
