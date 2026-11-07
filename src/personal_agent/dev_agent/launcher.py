"""The launcher of a job's agent container (ADR-0159 D3; FRE-1684).

One interface, :class:`AgentLauncher`, with one implementation, :class:`DockerLauncher`, which runs
the ``docker`` CLI through the gateway's Docker socket. A move to a separate VPS (D3.5) replaces
this implementation and the base URLs, not the job logic.

Each job gets a new container and a new workspace volume. Both carry the job id as the label
``seshat.dev_job`` and the supervisor scope as ``seshat.dev_job.scope``, so removal and the orphan
sweep find them by label (D9.5) and never touch another scope's containers. The
two job credentials (D6) reach ``docker create`` through the CLI's environment only (``-e NAME``
with no value), so they never show on a command line.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import structlog

from personal_agent.dev_agent.container_spec import (
    ContainerProfile,
    agent_container_name,
    build_create_args,
)
from personal_agent.dev_agent.toolkit import (
    ToolkitAsset,
    check_toolkit_contents,
    read_toolkit_volume,
)
from personal_agent.exceptions import DevAgentLaunchError

log = structlog.get_logger(__name__)

#: The label that carries the job id on every container and volume of a job.
JOB_LABEL = "seshat.dev_job"
#: The label that names the supervisor scope. Every listing filters on it, so a test supervisor and
#: the production supervisor, which share one Docker host, never see each other's containers.
SCOPE_LABEL = "seshat.dev_job.scope"
#: The command of the agent container. The image's default command is the network probe.
OPENCODE_COMMAND: tuple[str, ...] = ("opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096")
#: Each Docker CLI call ends within this time.
DOCKER_TIMEOUT_SECONDS = 60.0


def workspace_volume_name(job_id: UUID) -> str:
    """Return the name of a job's workspace volume.

    Args:
        job_id: The job.

    Returns:
        A Docker volume name.
    """
    return f"seshat-dev-ws-{job_id}"


@dataclass(frozen=True)
class CommandResult:
    """The result of one CLI call.

    Attributes:
        returncode: The exit status.
        stdout: Standard output.
        stderr: Standard error.
    """

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Runs one command with an explicit argument list, never through a shell."""

    async def __call__(
        self, args: Sequence[str], *, env: Mapping[str, str] | None = None
    ) -> CommandResult:
        """Run ``args`` and return its result."""
        ...


async def run_command(
    args: Sequence[str], *, env: Mapping[str, str] | None = None
) -> CommandResult:
    """Run a command, bounded by :data:`DOCKER_TIMEOUT_SECONDS`.

    Args:
        args: The argument list.
        env: The whole environment of the process, or ``None`` to inherit the gateway's.

    Returns:
        The result. A timeout kills the process and returns exit status 124.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        env=dict(env) if env is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=DOCKER_TIMEOUT_SECONDS)
    except TimeoutError:
        await _kill(proc)
        return CommandResult(124, "", f"timed out after {DOCKER_TIMEOUT_SECONDS} s")
    except BaseException:
        # A cancelled caller must not leave the CLI running: it could act after the fence ended.
        await asyncio.shield(_kill(proc))
        raise
    return CommandResult(
        proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
    )


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill a child process, if it still runs, and reap it."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    await proc.wait()


class AgentLauncher(Protocol):
    """Starts, stops and removes a job's containers."""

    async def start(self, job_id: UUID, *, job_token: str, server_password: str) -> None:
        """Start the job's agent container with a new workspace volume."""
        ...

    async def stop(self, job_id: UUID) -> None:
        """Stop the job's agent container. Its workspace volume stays for the handoff."""
        ...

    async def remove(self, job_id: UUID) -> None:
        """Remove every container and volume of the job, then verify that none remains."""
        ...

    async def labelled_job_ids(self) -> set[UUID]:
        """Return the job ids on every container and volume that carries the job label."""
        ...


class DockerLauncher:
    """The local Docker implementation of :class:`AgentLauncher`."""

    def __init__(
        self,
        profile: ContainerProfile,
        *,
        network_name: str,
        toolkit_volume: str,
        toolkit_assets: Sequence[ToolkitAsset],
        scope: str,
        runner: CommandRunner = run_command,
    ) -> None:
        """Create the launcher.

        Args:
            profile: The ``agent`` profile of ``container_profiles.yaml``.
            network_name: The real name of the ``dev-agent`` network (project-prefixed).
            toolkit_volume: The named volume that holds the pinned toolkit. It must exist.
            toolkit_assets: The pins that the volume's files must match before each job (FRE-1751).
            scope: The supervisor scope, put on every container and volume and used in every listing.
            runner: Runs the ``docker`` CLI. Tests pass a fake.
        """
        self._profile = profile
        self._network = network_name
        self._toolkit = toolkit_volume
        self._toolkit_assets = tuple(toolkit_assets)
        self._scope = scope
        self._run = runner

    async def _docker(self, *args: str, env: Mapping[str, str] | None = None) -> CommandResult:
        return await self._run(["docker", *args], env=env)

    async def _require(self, what: str, *args: str, env: Mapping[str, str] | None = None) -> str:
        result = await self._docker(*args, env=env)
        if result.returncode != 0:
            raise DevAgentLaunchError(
                f"{what} failed ({result.returncode}): {result.stderr.strip()[:500]}"
            )
        return result.stdout

    async def start(self, job_id: UUID, *, job_token: str, server_password: str) -> None:
        """Start the job's agent container (D3.1–D3.3).

        Args:
            job_id: The job.
            job_token: The job token (D6). It reaches the container through the CLI environment.
            server_password: The new OpenCode server password (D6).

        Raises:
            DevAgentLaunchError: If the toolkit volume is missing or a Docker call fails.
            DevAgentToolkitError: If a file of the toolkit volume is missing, changed or extra.
        """
        # Never create the toolkit volume here: Docker would create an empty one, and a missing
        # toolkit would look like a present one.
        await self._require("toolkit volume check", "volume", "inspect", self._toolkit)
        # The volume is checked by its bytes before every job: a half-copied or edited volume
        # must not reach an agent (FRE-1751).
        check_toolkit_contents(
            self._toolkit_assets,
            await read_toolkit_volume(self._toolkit, self._profile.image, self._run),
        )
        workspace = workspace_volume_name(job_id)
        await self._require(
            "workspace volume create",
            "volume",
            "create",
            "--label",
            f"{JOB_LABEL}={job_id}",
            "--label",
            f"{SCOPE_LABEL}={self._scope}",
            workspace,
        )
        name = agent_container_name(job_id)
        args = build_create_args(
            self._profile,
            name=name,
            network_name=self._network,
            labels={JOB_LABEL: str(job_id), SCOPE_LABEL: self._scope, f"{JOB_LABEL}.kind": "agent"},
            volumes={"/workspace": workspace, "/srv/artifact-lib": self._toolkit},
        )
        env = dict(os.environ)
        env["SESHAT_JOB_TOKEN"] = job_token
        env["OPENCODE_SERVER_PASSWORD"] = server_password
        await self._require("docker create", *args[1:], *OPENCODE_COMMAND, env=env)
        await self._require("docker start", "start", name)
        log.info("dev_agent_container_started", job_id=str(job_id), container=name, trace_id=None)

    async def stop(self, job_id: UUID) -> None:
        """Stop the agent container, if it exists.

        Args:
            job_id: The job.

        Raises:
            DevAgentLaunchError: If ``docker stop`` fails for a container that exists.
        """
        result = await self._docker("stop", "-t", "10", agent_container_name(job_id))
        if result.returncode != 0 and "No such container" not in result.stderr:
            raise DevAgentLaunchError(f"docker stop failed: {result.stderr.strip()[:500]}")

    def _filters(self, label: str) -> list[str]:
        """Return ``--filter`` options for ``label`` within this scope. Docker ANDs them."""
        return ["--filter", f"label={label}", "--filter", f"label={SCOPE_LABEL}={self._scope}"]

    async def _listed(self, kind: str, label_filter: str) -> list[str]:
        if kind == "container":
            out = await self._require("container list", "ps", "-aq", *self._filters(label_filter))
        else:
            out = await self._require(
                "volume list", "volume", "ls", "-q", *self._filters(label_filter)
            )
        return [line for line in out.split() if line]

    async def remove(self, job_id: UUID) -> None:
        """Remove every container and volume of the job, and verify that none remains (D3.4).

        Args:
            job_id: The job.

        Raises:
            DevAgentLaunchError: If a Docker call fails, or a container or volume remains.
        """
        label = f"{JOB_LABEL}={job_id}"
        containers = await self._listed("container", label)
        if containers:
            await self._require("container remove", "rm", "-f", *containers)
        volumes = await self._listed("volume", label)
        if volumes:
            await self._require("volume remove", "volume", "rm", "-f", *volumes)
        left = await self._listed("container", label) + await self._listed("volume", label)
        if left:
            raise DevAgentLaunchError(f"job {job_id} still has {left} after removal")
        log.info(
            "dev_agent_job_removed",
            job_id=str(job_id),
            containers=len(containers),
            volumes=len(volumes),
            trace_id=None,
        )

    async def labelled_job_ids(self) -> set[UUID]:
        """Return the job ids on every labelled container and volume of this scope.

        Returns:
            The ids. A label value that is not a UUID is skipped.
        """
        template = '{{.Label "' + JOB_LABEL + '"}}'
        filters = self._filters(JOB_LABEL)
        containers = await self._require(
            "container list", "ps", "-a", *filters, "--format", template
        )
        volumes = await self._require("volume list", "volume", "ls", *filters, "--format", template)
        found: set[UUID] = set()
        for value in (containers + "\n" + volumes).split():
            try:
                found.add(UUID(value))
            except ValueError:
                continue
        return found
