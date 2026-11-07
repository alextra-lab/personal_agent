# ruff: noqa: D101, D102
"""The Docker launcher's calls (ADR-0159 D3.1–D3.4, D6, D8.4, D8.8.2; FRE-1684, FRE-1685).

A fake runner records every ``docker`` call. The live behaviour is in ``test_supervisor_live.py``.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Mapping, Sequence
from uuid import uuid4

import pytest

from personal_agent.dev_agent.container_spec import agent_container_name, load_profiles
from personal_agent.dev_agent.launcher import (
    JOB_LABEL,
    OPENCODE_COMMAND,
    SCOPE_LABEL,
    BytesResult,
    CommandResult,
    DockerLauncher,
    handoff_container_name,
    handoff_volume_name,
    parse_single_file_tar,
    run_bytes,
    workspace_volume_name,
)
from personal_agent.exceptions import DevAgentLaunchError, DevAgentToolkitError

from .toolkit_fakes import (
    FakeDocker,
    complete_volume,
    synthetic_assets,
)

pytestmark = pytest.mark.asyncio

_TOKEN = "job-token-secret-value"
_PASSWORD = "server-password-secret-value"


def _fake() -> FakeDocker:
    """A fake Docker whose ``toolkit`` volume holds the correct synthetic toolkit."""
    fake = FakeDocker()
    fake.volumes["toolkit"] = complete_volume()
    return fake


def _launcher(fake: FakeDocker) -> DockerLauncher:
    return DockerLauncher(
        load_profiles().agent,
        network_name="seshat_dev-agent",
        toolkit_volume="toolkit",
        toolkit_assets=synthetic_assets(),
        scope="prod",
        runner=fake,
    )


class TestStart:
    async def test_the_container_is_created_from_the_spec_with_the_job_label(self) -> None:
        fake = _fake()
        job = uuid4()
        await _launcher(fake).start(job, job_token=_TOKEN, server_password=_PASSWORD)
        commands = [argv[1:3] for argv, _ in fake.calls]
        helper = fake.calls[1][0][fake.calls[1][0].index("--name") + 1]
        assert helper.startswith("seshat-dev-toolkit-")
        assert commands == [
            ["volume", "inspect"],
            ["create", "--network"],  # the helper that reads the toolkit volume
            ["cp", f"{helper}:/srv/artifact-lib/."],
            ["rm", "-f"],
            ["volume", "create"],
            ["create", "--name"],
            ["start", agent_container_name(job)],
        ]
        create, env = fake.calls[5]
        assert create[create.index("--name") + 1] == agent_container_name(job)
        assert f"{JOB_LABEL}={job}" in create
        assert f"{SCOPE_LABEL}=prod" in create
        assert f"{SCOPE_LABEL}=prod" in fake.calls[4][0]
        assert create[create.index("--network") + 1] == "seshat_dev-agent"
        assert f"{workspace_volume_name(job)}:/workspace" in create
        assert "toolkit:/srv/artifact-lib:ro" in create
        assert tuple(create[-len(OPENCODE_COMMAND) :]) == OPENCODE_COMMAND
        assert create[-len(OPENCODE_COMMAND) - 1] == "seshat-dev-agent:0.1"
        assert env is not None
        assert env["SESHAT_JOB_TOKEN"] == _TOKEN
        assert env["OPENCODE_SERVER_PASSWORD"] == _PASSWORD

    async def test_no_secret_is_on_any_command_line(self) -> None:
        fake = _fake()
        await _launcher(fake).start(uuid4(), job_token=_TOKEN, server_password=_PASSWORD)
        for argv, _ in fake.calls:
            assert not any(_TOKEN in arg or _PASSWORD in arg for arg in argv)
        create = fake.calls[5][0]
        assert "SESHAT_JOB_TOKEN" in create and "OPENCODE_SERVER_PASSWORD" in create

    async def test_a_missing_toolkit_volume_starts_nothing(self) -> None:
        fake = _fake()
        fake.fail.add("volume inspect")
        with pytest.raises(DevAgentLaunchError, match="toolkit"):
            await _launcher(fake).start(uuid4(), job_token=_TOKEN, server_password=_PASSWORD)
        assert [argv[1:3] for argv, _ in fake.calls] == [["volume", "inspect"]]

    async def test_a_failed_create_raises(self) -> None:
        fake = _fake()
        fake.fail.add("create --name")
        with pytest.raises(DevAgentLaunchError, match="docker create"):
            await _launcher(fake).start(uuid4(), job_token=_TOKEN, server_password=_PASSWORD)

    @pytest.mark.parametrize(
        ("damage", "message"),
        [
            ("change", "differs from the pin"),
            ("extra", "unexpected file"),
            ("half", "missing"),
            ("empty", "missing"),
        ],
    )
    async def test_a_wrong_toolkit_volume_starts_nothing(self, damage: str, message: str) -> None:
        fake = _fake()
        files = fake.volumes["toolkit"]
        first = sorted(files)[0]
        if damage == "change":
            files[first] += b"!"
        elif damage == "extra":
            files["planted.js"] = b"evil()"
        elif damage == "half":
            del files[sorted(files)[-1]]
        else:
            files.clear()
        with pytest.raises(DevAgentToolkitError, match=message):
            await _launcher(fake).start(uuid4(), job_token=_TOKEN, server_password=_PASSWORD)
        assert ["volume", "create"] not in [argv[1:3] for argv in fake.argvs()]
        assert not any(argv[1] == "start" for argv in fake.argvs())
        assert fake.containers == {}, "the helper container is removed"


class TestRemove:
    async def test_it_removes_by_label_and_verifies(self) -> None:
        fake = FakeDocker()
        job = uuid4()
        fake.listing = {"ps -aq": ["c1"], "volume ls": ["v1"]}
        await _launcher(fake).remove(job)
        argvs = [argv for argv, _ in fake.calls]
        assert ["docker", "rm", "-f", "c1"] in argvs
        assert ["docker", "volume", "rm", "-f", "v1"] in argvs
        listings = [argv for argv in argvs if argv[1] == "ps" or argv[1:3] == ["volume", "ls"]]
        assert len(listings) == 4
        assert all(f"label={JOB_LABEL}={job}" in argv for argv in listings)
        assert all(f"label={SCOPE_LABEL}=prod" in argv for argv in listings)

    async def test_a_remaining_container_raises(self) -> None:
        job = uuid4()

        async def sticky(
            args: Sequence[str], *, env: Mapping[str, str] | None = None
        ) -> CommandResult:
            if list(args[1:3]) == ["ps", "-aq"]:
                return CommandResult(0, "stuck", "")
            return CommandResult(0, "", "")

        launcher = DockerLauncher(
            load_profiles().agent,
            network_name="n",
            toolkit_volume="t",
            toolkit_assets=synthetic_assets(),
            scope="prod",
            runner=sticky,
        )
        with pytest.raises(DevAgentLaunchError, match="still has"):
            await launcher.remove(job)


class TestLabels:
    async def test_it_parses_job_ids_from_containers_and_volumes(self) -> None:
        a, b = uuid4(), uuid4()

        async def listing(
            args: Sequence[str], *, env: Mapping[str, str] | None = None
        ) -> CommandResult:
            if args[1] == "ps":
                return CommandResult(0, f"{a}\nnot-a-uuid\n", "")
            return CommandResult(0, f"{a}\n{b}\n", "")

        launcher = DockerLauncher(
            load_profiles().agent,
            network_name="n",
            toolkit_volume="t",
            toolkit_assets=synthetic_assets(),
            scope="prod",
            runner=listing,
        )
        assert await launcher.labelled_job_ids() == {a, b}


class TestRunCommand:
    async def test_a_cancelled_call_kills_and_reaps_its_child(self) -> None:
        """Review finding 5: a cancelled caller leaves no CLI process behind."""
        import asyncio

        import psutil  # type: ignore[import-untyped]

        from personal_agent.dev_agent.launcher import run_command

        marker = "31.4159"
        task = asyncio.create_task(run_command(["sleep", marker]))
        await asyncio.sleep(0.3)
        children = [p for p in psutil.Process().children() if p.cmdline()[-1:] == [marker]]
        assert len(children) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not children[0].is_running() or children[0].status() == psutil.STATUS_ZOMBIE
        assert not [p for p in psutil.Process().children() if p.cmdline()[-1:] == [marker]]


class FakeBytes:
    """Records every binary ``docker`` call. ``cp`` from a container returns ``tar``."""

    def __init__(self, tar: bytes = b"", start_status: int = 0) -> None:
        self.calls: list[tuple[list[str], bytes | None, float]] = []
        self.tar = tar
        self.start_status = start_status

    async def __call__(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float = 60.0,
        max_stdout: int = 1 << 26,
    ) -> BytesResult:
        argv = list(args)
        self.calls.append((argv, stdin, timeout))
        if argv[1] == "start":
            return BytesResult(self.start_status, b"", "seeded" if self.start_status else "")
        if argv[1] == "cp" and argv[-1] == "-":
            return BytesResult(0, self.tar, "")
        return BytesResult(0, b"", "")


def _tar_of(name: str, data: bytes, kind: bytes = tarfile.REGTYPE) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.type = kind
        if kind == tarfile.SYMTYPE:
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        else:
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _two_files() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in ("job.bundle", "extra"):
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    return buffer.getvalue()


def _full_launcher(fake: FakeDocker, raw: FakeBytes) -> DockerLauncher:
    profiles = load_profiles()
    return DockerLauncher(
        profiles.agent,
        network_name="seshat_dev-agent",
        toolkit_volume="toolkit",
        scope="prod",
        handoff_profile=profiles.handoff,
        runner=fake,
        bytes_runner=raw,
    )


class TestWorkspaceCopy:
    async def test_the_clone_is_copied_in_between_create_and_start(self) -> None:
        fake, raw = FakeDocker(), FakeBytes()
        job = uuid4()
        await _full_launcher(fake, raw).start(
            job, job_token=_TOKEN, server_password=_PASSWORD, workspace=b"TAR"
        )
        (cp, stdin, _), = raw.calls
        assert cp == ["docker", "cp", "-a", "-", f"{agent_container_name(job)}:/workspace"]
        assert stdin == b"TAR"
        # The copy came after the create and before the start.
        order = [argv[1] for argv, _ in fake.calls]
        assert order == ["volume", "volume", "create", "start"]


class TestBundleWorkspace:
    async def test_the_handoff_container_has_the_profile_and_returns_the_bundle(self) -> None:
        job = uuid4()
        fake, raw = FakeDocker(), FakeBytes(tar=_tar_of("job.bundle", b"BUNDLE"))
        bundle = await _full_launcher(fake, raw).bundle_workspace(job, f"job/{job}")
        assert bundle == b"BUNDLE"
        create = next(argv for argv, _ in fake.calls if argv[1] == "create")
        assert create[create.index("--name") + 1] == handoff_container_name(job)
        assert create[create.index("--network") + 1] == "none"
        assert f"{workspace_volume_name(job)}:/workspace:ro" in create
        assert f"{handoff_volume_name(job)}:/out" in create
        assert f"{JOB_LABEL}={job}" in create and f"{SCOPE_LABEL}=prod" in create
        assert create[-2:] == ["seshat-dev-handoff:0.1", f"job/{job}"]
        start = next(c for c in raw.calls if c[0][1] == "start")
        assert start[0] == ["docker", "start", "-a", handoff_container_name(job)]
        assert start[2] == 300.0
        # Removed after, container and output volume.
        tail = [argv[1:3] for argv, _ in fake.calls[-2:]]
        assert tail == [["rm", "-f"], ["volume", "rm"]]

    async def test_a_failed_handoff_raises_and_still_cleans_up(self) -> None:
        job = uuid4()
        fake, raw = FakeDocker(), FakeBytes(start_status=3)
        with pytest.raises(DevAgentLaunchError, match="handoff run"):
            await _full_launcher(fake, raw).bundle_workspace(job, f"job/{job}")
        assert [argv[1:3] for argv, _ in fake.calls[-2:]] == [["rm", "-f"], ["volume", "rm"]]

    @pytest.mark.parametrize(
        "tar",
        [
            _tar_of("job.bundle", b"x", tarfile.SYMTYPE),
            _tar_of("other", b"x"),
            _two_files(),
            b"not a tar",
        ],
    )
    async def test_only_one_regular_bundle_file_is_accepted(self, tar: bytes) -> None:
        job = uuid4()
        with pytest.raises(DevAgentLaunchError):
            await _full_launcher(FakeDocker(), FakeBytes(tar=tar)).bundle_workspace(
                job, f"job/{job}"
            )


async def test_parse_single_file_tar_refuses_an_oversized_member() -> None:
    with pytest.raises(DevAgentLaunchError):
        parse_single_file_tar(_tar_of("job.bundle", b"x" * 11), "job.bundle", 10)


class TestRunBytes:
    async def test_stdin_and_stdout_are_binary(self) -> None:
        result = await run_bytes(["cat"], stdin=b"\x00\xffdata")
        assert (result.returncode, result.stdout) == (0, b"\x00\xffdata")

    async def test_output_past_the_cap_is_refused(self) -> None:
        result = await run_bytes(["head", "-c", "5000", "/dev/zero"], max_stdout=100)
        assert (result.returncode, result.stdout) == (125, b"")

    async def test_output_past_the_cap_kills_a_child_that_keeps_running(self) -> None:
        """Code review finding 1: the cap ends the call at once, not at the timeout."""
        import time

        script = "import sys, time; sys.stdout.write('x' * 200000); sys.stdout.flush(); time.sleep(30)"
        started = time.monotonic()
        result = await run_bytes(["python3", "-c", script], max_stdout=1000, timeout=20)
        assert (result.returncode, result.stdout) == (125, b"")
        assert time.monotonic() - started < 5

    async def test_a_timeout_returns_124(self) -> None:
        result = await run_bytes(["sleep", "5"], timeout=0.2)
        assert result.returncode == 124

    async def test_large_stderr_does_not_block(self) -> None:
        script = "import sys; sys.stderr.write('e' * 300000); print('done')"
        result = await run_bytes(["python3", "-c", script], timeout=10)
        assert result.stdout.strip() == b"done"
        assert len(result.stderr) == 65536
