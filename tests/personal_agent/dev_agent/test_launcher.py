# ruff: noqa: D101, D102
"""The Docker launcher's calls (ADR-0159 D3.1–D3.4, D6; FRE-1684).

A fake runner records every ``docker`` call. The live behaviour is in ``test_supervisor_live.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import uuid4

import pytest

from personal_agent.dev_agent.container_spec import agent_container_name, load_profiles
from personal_agent.dev_agent.launcher import (
    JOB_LABEL,
    OPENCODE_COMMAND,
    SCOPE_LABEL,
    CommandResult,
    DockerLauncher,
    workspace_volume_name,
)
from personal_agent.exceptions import DevAgentLaunchError

pytestmark = pytest.mark.asyncio

_TOKEN = "job-token-secret-value"
_PASSWORD = "server-password-secret-value"


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Mapping[str, str] | None]] = []
        self.fail: set[str] = set()
        self.listing: dict[str, list[str]] = {}

    async def __call__(
        self, args: Sequence[str], *, env: Mapping[str, str] | None = None
    ) -> CommandResult:
        argv = list(args)
        self.calls.append((argv, env))
        key = " ".join(argv[1:3])
        if key in self.fail:
            return CommandResult(1, "", f"{key} broke")
        if argv[1] == "ps" or argv[1:3] == ["volume", "ls"]:
            return CommandResult(0, "\n".join(self.listing.pop(key, [])), "")
        return CommandResult(0, "", "")


def _launcher(fake: FakeDocker) -> DockerLauncher:
    return DockerLauncher(
        load_profiles().agent,
        network_name="seshat_dev-agent",
        toolkit_volume="toolkit",
        scope="prod",
        runner=fake,
    )


class TestStart:
    async def test_the_container_is_created_from_the_spec_with_the_job_label(self) -> None:
        fake = FakeDocker()
        job = uuid4()
        await _launcher(fake).start(job, job_token=_TOKEN, server_password=_PASSWORD)
        commands = [argv[1:3] for argv, _ in fake.calls]
        assert commands == [
            ["volume", "inspect"],
            ["volume", "create"],
            ["create", "--name"],
            ["start", agent_container_name(job)],
        ]
        create, env = fake.calls[2]
        assert create[create.index("--name") + 1] == agent_container_name(job)
        assert f"{JOB_LABEL}={job}" in create
        assert f"{SCOPE_LABEL}=prod" in create
        assert f"{SCOPE_LABEL}=prod" in fake.calls[1][0]
        assert create[create.index("--network") + 1] == "seshat_dev-agent"
        assert f"{workspace_volume_name(job)}:/workspace" in create
        assert "toolkit:/srv/artifact-lib:ro" in create
        assert tuple(create[-len(OPENCODE_COMMAND) :]) == OPENCODE_COMMAND
        assert create[-len(OPENCODE_COMMAND) - 1] == "seshat-dev-agent:0.1"
        assert env is not None
        assert env["SESHAT_JOB_TOKEN"] == _TOKEN
        assert env["OPENCODE_SERVER_PASSWORD"] == _PASSWORD

    async def test_no_secret_is_on_any_command_line(self) -> None:
        fake = FakeDocker()
        await _launcher(fake).start(uuid4(), job_token=_TOKEN, server_password=_PASSWORD)
        for argv, _ in fake.calls:
            assert not any(_TOKEN in arg or _PASSWORD in arg for arg in argv)
        create = fake.calls[2][0]
        assert "SESHAT_JOB_TOKEN" in create and "OPENCODE_SERVER_PASSWORD" in create

    async def test_a_missing_toolkit_volume_starts_nothing(self) -> None:
        fake = FakeDocker()
        fake.fail.add("volume inspect")
        with pytest.raises(DevAgentLaunchError, match="toolkit"):
            await _launcher(fake).start(uuid4(), job_token=_TOKEN, server_password=_PASSWORD)
        assert [argv[1:3] for argv, _ in fake.calls] == [["volume", "inspect"]]

    async def test_a_failed_create_raises(self) -> None:
        fake = FakeDocker()
        fake.fail.add("create --name")
        with pytest.raises(DevAgentLaunchError, match="docker create"):
            await _launcher(fake).start(uuid4(), job_token=_TOKEN, server_password=_PASSWORD)


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
            load_profiles().agent, network_name="n", toolkit_volume="t", scope="prod", runner=sticky
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
            scope="prod",
            runner=listing,
        )
        assert await launcher.labelled_job_ids() == {a, b}
