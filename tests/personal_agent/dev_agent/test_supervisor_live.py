# ruff: noqa: D101, D102
"""The supervisor with real Docker containers (ADR-0159 AC-7, AC-8; FRE-1684 AC-1, AC-2, AC-4, AC-5).

Each test uses its own scope label, a throwaway internal network and a throwaway toolkit volume, so
it never touches a production container on the shared Docker host. The store runs on the test
stack. The relay is not in this path (it needs a running broker), so the agent is scripted: it acts
through ``docker exec`` in the real agent container, where ``opencode serve`` runs.

Times are short (lease 6 s, renew 2 s, stop after 4 s). The unit tests in
``test_supervisor_integration.py`` use the ADR's numbers. Image: ``SESHAT_DEV_AGENT_TEST_IMAGE``,
else ``seshat-dev-agent:fre1684-test``, else ``seshat-dev-agent:0.1``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import psutil  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from personal_agent.delegation.protocols import (
    DelegationExecutorProtocol,
    DelegationOutcome,
    DelegationPackage,
)
from personal_agent.dev_agent.container_spec import agent_container_name, load_profiles
from personal_agent.dev_agent.jobs import DevJob, JobStore
from personal_agent.dev_agent.launcher import (
    JOB_LABEL,
    SCOPE_LABEL,
    DockerLauncher,
    workspace_volume_name,
)
from personal_agent.dev_agent.supervisor import JobSupervisor, SupervisorPolicy
from personal_agent.telemetry.trace import TraceContext

from .conftest import MakeJob

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_CHILD = Path(__file__).with_name("live_child.py")
_POLICY = SupervisorPolicy(
    lease_seconds=6.0, renew_seconds=2.0, stop_after_seconds=4.0, scan_seconds=1.0
)

#: Runs inside the agent container. "mark" leaves a file in every writable path and an OpenCode
#: session. Both modes then report the markers and the sessions that they find.
_PROBE = r"""
import base64, json, os, sys, time, urllib.request
mode = sys.argv[1]
auth = base64.b64encode(("opencode:" + os.environ["OPENCODE_SERVER_PASSWORD"]).encode()).decode()
H = {"Authorization": "Basic " + auth, "content-type": "application/json"}
def call(method, path, body=None):
    req = urllib.request.Request("http://127.0.0.1:4096" + path, method=method, headers=H,
                                 data=json.dumps(body).encode() if body is not None else None)
    return json.loads(urllib.request.urlopen(req, timeout=5).read() or b"null")
for _ in range(90):
    try:
        call("GET", "/api/session")
        break
    except Exception:
        time.sleep(1)
roots = ["/workspace", "/tmp", os.environ["HOME"]]
if mode == "mark":
    for root in roots:
        open(os.path.join(root, "fre1684-marker"), "w").write("job 1")
    call("POST", "/api/session", {"title": "fre1684-job-1"})
found = [os.path.join(d, f) for root in roots for d, _, fs in os.walk(root) for f in fs
         if f.startswith("fre1684-marker")]
sessions = [s.get("title") for s in call("GET", "/api/session")["data"]]
print(json.dumps({"markers": sorted(found), "sessions": sessions}))
"""


def _docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=120, check=False
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)}: {result.stderr}")
    return result.stdout


def _image() -> str | None:
    for candidate in (
        os.environ.get("SESHAT_DEV_AGENT_TEST_IMAGE"),
        "seshat-dev-agent:fre1684-test",
        "seshat-dev-agent:0.1",
    ):
        if candidate and _docker("image", "inspect", candidate, check=False):
            return candidate
    return None


@dataclass
class LiveEnv:
    scope: str
    network: str
    toolkit: str
    image: str
    pool: asyncpg.Pool

    def launcher(self) -> DockerLauncher:
        profile = load_profiles().agent.model_copy(update={"image": self.image})
        return DockerLauncher(
            profile, network_name=self.network, toolkit_volume=self.toolkit, scope=self.scope
        )

    def leftovers(self, job_id: UUID) -> list[str]:
        """Every container and volume of the job that Docker still lists, by both labels."""
        filters = [
            "--filter",
            f"label={JOB_LABEL}={job_id}",
            "--filter",
            f"label={SCOPE_LABEL}={self.scope}",
        ]
        return (
            _docker("ps", "-aq", *filters).split() + _docker("volume", "ls", "-q", *filters).split()
        )

    async def row(self, job_id: UUID) -> asyncpg.Record:
        return await self.pool.fetchrow(
            "SELECT state, failure_reason, lease_epoch, lease_owner, lease_expires_at, started_at "
            "FROM dev_jobs WHERE id = $1",
            job_id,
        )

    async def token(self, job_id: UUID) -> str | None:
        return await self.pool.fetchval(
            "SELECT status FROM dev_job_tokens WHERE job_id = $1", job_id
        )

    async def actions(self, job_id: UUID) -> list[tuple[str, int, str, str | None]]:
        rows = await self.pool.fetch(
            "SELECT action, lease_epoch, lease_owner, outcome FROM dev_job_actions WHERE job_id = $1 ORDER BY id",
            job_id,
        )
        return [(r["action"], r["lease_epoch"], r["lease_owner"], r["outcome"]) for r in rows]

    def child(self, owner: str, pause_before: str | None = None) -> subprocess.Popen[bytes]:
        args = [
            sys.executable,
            str(_CHILD),
            self.scope,
            self.network,
            self.toolkit,
            self.image,
            owner,
        ]
        if pause_before:
            args.append(pause_before)
        # The parent's environment holds the test-stack redirects of tests/conftest.py.
        return subprocess.Popen(
            args, env=dict(os.environ), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )


@pytest_asyncio.fixture
async def live(admin_pool: asyncpg.Pool) -> AsyncIterator[LiveEnv]:
    if shutil.which("docker") is None:
        pytest.skip("no docker")
    image = _image()
    if image is None:
        pytest.skip("no dev-agent image: run make dev-agent-build")
    tag = uuid4().hex[:8]
    env = LiveEnv(
        scope=f"fre1684-test-{tag}",
        network=f"fre1684-net-{tag}",
        toolkit=f"fre1684-toolkit-{tag}",
        image=image,
        pool=admin_pool,
    )
    _docker("network", "create", "--internal", env.network)
    _docker("volume", "create", env.toolkit)
    try:
        yield env
    finally:
        scope = f"label={SCOPE_LABEL}={env.scope}"
        for container in _docker("ps", "-aq", "--filter", scope).split():
            _docker("rm", "-f", container, check=False)
        for volume in _docker("volume", "ls", "-q", "--filter", scope).split():
            _docker("volume", "rm", "-f", volume, check=False)
        _docker("network", "rm", env.network, check=False)
        _docker("volume", "rm", "-f", env.toolkit, check=False)


Script = Callable[[DevJob, float], Awaitable[DelegationOutcome]]


def _scripted(script: Script) -> Callable[[DevJob, str], AsyncIterator[DelegationExecutorProtocol]]:
    class _Adapter:
        def __init__(self, job: DevJob) -> None:
            self._job = job

        def available(self) -> bool:
            return True

        async def delegate(
            self, package: DelegationPackage, timeout: float, trace_ctx: TraceContext
        ) -> DelegationOutcome:
            del package, trace_ctx
            return await script(self._job, timeout)

    @asynccontextmanager
    async def factory(job: DevJob, password: str) -> AsyncIterator[DelegationExecutorProtocol]:
        del password
        yield _Adapter(job)

    return factory  # type: ignore[return-value]


async def _probe(job: DevJob, mode: str) -> dict[str, list[str]]:
    def run() -> str:
        result = subprocess.run(
            ["docker", "exec", "-i", agent_container_name(job.id), "python3", "-", mode],
            input=_PROBE,
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        )
        return result.stdout

    return json.loads(await asyncio.to_thread(run))


async def _until(condition: Callable[[], Awaitable[bool]], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not await condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.2)


def _supervisor(
    live: LiveEnv, owner: str, script: Script, policy: SupervisorPolicy = _POLICY
) -> JobSupervisor:
    return JobSupervisor(
        store=JobStore(live.pool),
        launcher=live.launcher(),
        adapter_factory=_scripted(script),
        policy=policy,
        owner=owner,
    )


async def _run_one(sup: JobSupervisor) -> None:
    await sup.scan_once()
    assert sup._current is not None
    await asyncio.wait_for(sup._current, timeout=240)


class TestRestart:
    async def test_a_gateway_killed_just_after_a_renewal_leaves_nothing_once_recovered(
        self, live: LiveEnv, make_job: MakeJob
    ) -> None:
        """AC-1 live: SIGKILL just after a renewal → failed/supervisor_restart, nothing left."""
        job = await make_job()
        child = live.child("child-a")
        try:
            await _until(lambda: _started(live, job.id), 120)
            first = (await live.row(job.id))["lease_expires_at"]
            await _until(lambda: _renewed_after(live, job.id, first), 30)
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
        assert live.leftovers(job.id), "the killed gateway left its container behind"
        killed_at = (await live.row(job.id))["lease_expires_at"]

        recovery = _supervisor(live, "gateway-b", _never_reached)
        await recovery.scan_once()
        assert (await live.row(job.id))["state"] == "running", "nothing happens before the expiry"

        await _until(lambda: _db_time_after(live, killed_at), 30)
        await recovery.scan_once()
        row = await live.row(job.id)
        assert (row["state"], row["failure_reason"], row["lease_epoch"]) == (
            "failed",
            "supervisor_restart",
            2,
        )
        assert live.leftovers(job.id) == []
        assert await live.token(job.id) == "revoked"


class TestPausedSupervisor:
    async def test_a_supervisor_paused_past_its_lease_does_nothing_after_it_resumes(
        self, live: LiveEnv, make_job: MakeJob
    ) -> None:
        """AC-2 live: SIGSTOP before the container start, recovery, SIGCONT → no action."""
        job = await make_job()
        child = live.child("child-a", pause_before="start_container")
        try:
            process = psutil.Process(child.pid)
            await _until(lambda: _stopped(process), 120)
            paused_at = (await live.row(job.id))["lease_expires_at"]
            await _until(lambda: _db_time_after(live, paused_at), 30)
            recovery = _supervisor(live, "gateway-b", _never_reached)
            await recovery.scan_once()
            assert (await live.row(job.id))["state"] == "failed"
            actions = await live.actions(job.id)
            token = await live.token(job.id)
            resumed = int(time.time())

            child.send_signal(signal.SIGCONT)
            await asyncio.sleep(8)  # more than the child's scan, renew and stop times
            assert child.poll() is None, "the child keeps running: it only lost this job"
        finally:
            child.kill()
            child.wait(timeout=10)
        events = _job_events(job.id, resumed)
        assert events == [], f"Docker saw an action for the job after the resume: {events}"
        assert live.leftovers(job.id) == []
        assert await live.actions(job.id) == actions
        assert [owner for _, _, owner, _ in actions] == ["child-a", "gateway-b"]
        assert await live.token(job.id) == token == "revoked"
        row = await live.row(job.id)
        assert (row["state"], row["lease_epoch"], row["lease_owner"]) == ("failed", 2, "gateway-b")


class TestLimit:
    async def test_a_job_past_its_limit_ends_timed_out_with_no_container(
        self, live: LiveEnv, make_job: MakeJob
    ) -> None:
        """AC-4 live, with a 5 s limit: the agent runs, the limit passes, the job ends timed_out."""
        job = await make_job()
        seen: list[list[str]] = []

        async def runs_until_the_deadline(job_: DevJob, timeout: float) -> DelegationOutcome:
            seen.append(live.leftovers(job_.id))
            await asyncio.sleep(timeout)
            return DelegationOutcome(str(job_.id), False, 0, "", "timed_out")

        policy = SupervisorPolicy(
            lease_seconds=6.0, renew_seconds=2.0, stop_after_seconds=4.0, time_limit_seconds=5.0
        )
        since = int(time.time()) - 1
        await _run_one(_supervisor(live, "gateway-a", runs_until_the_deadline, policy))
        assert len(seen[0]) == 2, "the container and its workspace volume existed while it ran"
        row = await live.row(job.id)
        assert (row["state"], row["failure_reason"]) == ("timed_out", "time_limit")
        assert live.leftovers(job.id) == []
        assert await live.token(job.id) == "revoked"
        # Positive control of the instrument that the paused-supervisor test reads as "nothing".
        events = _job_events(job.id, since)
        for expected in (
            "volume create",
            "container create",
            "container start",
            "container destroy",
            "volume destroy",
        ):
            assert expected in events, (expected, events)


class TestConsecutiveJobs:
    async def test_the_second_job_finds_no_file_or_session_of_the_first(
        self, live: LiveEnv, make_job: MakeJob
    ) -> None:
        """AC-5 live: a fresh container, workspace, tmpfs and OpenCode state for each job."""
        results: dict[str, dict[str, list[str]]] = {}

        def probing(mode: str) -> Script:
            async def script(job: DevJob, timeout: float) -> DelegationOutcome:
                del timeout
                results[mode] = await _probe(job, mode)
                return DelegationOutcome(str(job.id), True, 1, "", "")

            return script

        first = await make_job()
        await _run_one(_supervisor(live, "gateway-a", probing("mark")))
        second = await make_job()
        await _run_one(_supervisor(live, "gateway-a", probing("check")))

        # The probe works: in the first job it sees its own markers and session.
        assert len(results["mark"]["markers"]) == 3
        assert results["mark"]["sessions"] == ["fre1684-job-1"]
        assert results["check"] == {"markers": [], "sessions": []}
        assert live.leftovers(first.id) == [] and live.leftovers(second.id) == []


def _job_events(job_id: UUID, since: int) -> list[str]:
    """Docker events of the job's containers (by label) and of its workspace volume (by name).

    Volume events carry no labels, so the label filter alone would miss a volume action.
    """
    until = str(int(time.time()) + 1)
    fmt = ["--format", "{{.Type}} {{.Action}}"]
    containers = _docker(
        "events",
        "--since",
        str(since),
        "--until",
        until,
        "--filter",
        f"label={JOB_LABEL}={job_id}",
        *fmt,
    )
    volumes = _docker(
        "events",
        "--since",
        str(since),
        "--until",
        until,
        "--filter",
        "type=volume",
        "--filter",
        f"volume={workspace_volume_name(job_id)}",
        *fmt,
    )
    return [line for line in (containers + volumes).split("\n") if line]


async def _never_reached(job: DevJob, timeout: float) -> DelegationOutcome:
    raise AssertionError(f"recovery never runs an agent ({job.id}, {timeout})")


async def _started(live: LiveEnv, job_id: UUID) -> bool:
    row = await live.row(job_id)
    return row is not None and row["started_at"] is not None


async def _renewed_after(live: LiveEnv, job_id: UUID, previous: object) -> bool:
    expires = (await live.row(job_id))["lease_expires_at"]
    return expires is not None and expires != previous


async def _db_time_after(live: LiveEnv, instant: object) -> bool:
    return bool(await live.pool.fetchval("SELECT clock_timestamp() > $1", instant))


async def _stopped(process: psutil.Process) -> bool:
    return process.status() == psutil.STATUS_STOPPED
