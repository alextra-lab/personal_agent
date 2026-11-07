# ruff: noqa: D102
"""The launcher spec for the dev-agent containers (ADR-0159 D3.3, D7.3, D8.8; FRE-1682)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from personal_agent.dev_agent.container_spec import (
    ContainerProfile,
    ContainerProfiles,
    build_create_args,
    load_profiles,
)

_VOLUMES = {"/workspace": "job-ws-1", "/srv/artifact-lib": "toolkit-1"}


@pytest.fixture(scope="module")
def profiles() -> ContainerProfiles:
    """The committed spec."""
    return load_profiles()


def _flag_values(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]


def _agent_args(profiles: ContainerProfiles) -> list[str]:
    return build_create_args(
        profiles.agent,
        name="seshat-dev-agent-job1",
        network_name="seshat_dev-agent",
        labels={"seshat.dev_agent.job": "job1"},
        volumes=_VOLUMES,
    )


class TestAgentProfileIsD33:
    """D3.3: the container boundary of the agent."""

    def test_non_root_user(self, profiles: ContainerProfiles) -> None:
        assert profiles.agent.user == "10001:10001"

    def test_limits(self, profiles: ContainerProfiles) -> None:
        agent = profiles.agent
        assert (agent.memory_mb, agent.cpus) == (3072, 2.0)
        assert agent.pids_limit > 0

    def test_capabilities_dropped_and_no_new_privileges(self, profiles: ContainerProfiles) -> None:
        assert profiles.agent.cap_drop == ("ALL",)
        assert profiles.agent.no_new_privileges is True

    def test_read_only_root_with_declared_writable_paths(self, profiles: ContainerProfiles) -> None:
        agent = profiles.agent
        assert agent.read_only_root is True
        assert {t.path for t in agent.tmpfs} == {"/tmp", "/devagent-home"}
        writable = {m.target for m in agent.mounts if not m.read_only}
        assert writable == {"/workspace"}

    def test_toolkit_mount_is_read_only(self, profiles: ContainerProfiles) -> None:
        readonly = {m.target for m in profiles.agent.mounts if m.read_only}
        assert readonly == {"/srv/artifact-lib"}

    def test_environment_is_a_declared_allowlist(self, profiles: ContainerProfiles) -> None:
        agent = profiles.agent
        assert set(agent.env_from_launcher) == {"SESHAT_JOB_TOKEN", "OPENCODE_SERVER_PASSWORD"}
        for name in ("PATH", "HOME", "LANG", "TZ", "NODE_ENV", "OPENCODE_CONFIG", "HTTPS_PROXY"):
            assert name in agent.env_fixed

    def test_runs_on_the_dev_agent_network(self, profiles: ContainerProfiles) -> None:
        assert profiles.agent.network == "dev-agent"


class TestCheckAndHandoffProfiles:
    """D7.3 and D8.8: the check and handoff containers have no network."""

    def test_check_container(self, profiles: ContainerProfiles) -> None:
        check = profiles.check
        assert check.network == "none"
        assert (check.memory_mb, check.cpus, check.wall_clock_seconds) == (2048, 1.0, 300)
        assert check.read_only_root is True
        assert check.cap_drop == ("ALL",)
        assert check.no_new_privileges is True
        assert [m.read_only for m in check.mounts] == [True] * len(check.mounts)
        assert [t.path for t in check.tmpfs] == ["/tmp"]  # D7.3: one bounded tmpfs

    def test_handoff_has_check_limits_and_hostile_repo_defaults(
        self, profiles: ContainerProfiles
    ) -> None:
        handoff = profiles.handoff
        check = profiles.check
        assert handoff.network == "none"
        assert (handoff.memory_mb, handoff.cpus) == (check.memory_mb, check.cpus)
        assert handoff.wall_clock_seconds == check.wall_clock_seconds
        assert handoff.env_fixed["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert handoff.env_fixed["GIT_CONFIG_SYSTEM"] == "/dev/null"
        workspace = next(m for m in handoff.mounts if m.target == "/workspace")
        assert workspace.read_only is True


class TestCreateArgs:
    """build_create_args renders a profile into docker create arguments."""

    def test_carries_every_boundary_flag(self, profiles: ContainerProfiles) -> None:
        args = _agent_args(profiles)
        assert args[:2] == ["docker", "create"]
        assert _flag_values(args, "--cap-drop") == ["ALL"]
        assert _flag_values(args, "--security-opt") == ["no-new-privileges"]
        assert _flag_values(args, "--memory") == ["3072m"]
        assert _flag_values(args, "--memory-swap") == ["3072m"]
        assert _flag_values(args, "--cpus") == ["2.0"]
        assert _flag_values(args, "--network") == ["seshat_dev-agent"]
        assert _flag_values(args, "-u") == ["10001:10001"]
        assert "--read-only" in args
        assert "--privileged" not in args
        assert args[-1] == profiles.agent.image

    def test_mounts_are_named_volumes_only(self, profiles: ContainerProfiles) -> None:
        args = _agent_args(profiles)
        assert sorted(_flag_values(args, "-v")) == [
            "job-ws-1:/workspace",
            "toolkit-1:/srv/artifact-lib:ro",
        ]
        assert "docker.sock" not in " ".join(args)

    def test_none_network_ignores_the_network_name(self, profiles: ContainerProfiles) -> None:
        args = build_create_args(
            profiles.check,
            name="seshat-dev-check-job1",
            network_name="seshat_dev-agent",
            labels={},
            volumes={m.target: f"vol-{i}" for i, m in enumerate(profiles.check.mounts)},
        )
        assert _flag_values(args, "--network") == ["none"]

    def test_launcher_credentials_never_appear_with_a_value(
        self, profiles: ContainerProfiles
    ) -> None:
        envs = _flag_values(_agent_args(profiles), "-e")
        assert "SESHAT_JOB_TOKEN" in envs
        assert "OPENCODE_SERVER_PASSWORD" in envs
        assert not any(
            e.startswith(("SESHAT_JOB_TOKEN=", "OPENCODE_SERVER_PASSWORD=")) for e in envs
        )

    def test_tmpfs_is_owned_by_the_container_user(self, profiles: ContainerProfiles) -> None:
        options = " ".join(_flag_values(_agent_args(profiles), "--tmpfs"))
        assert "uid=10001" in options and "gid=10001" in options

    def test_a_missing_volume_is_an_error(self, profiles: ContainerProfiles) -> None:
        with pytest.raises(ValueError, match="/workspace"):
            build_create_args(
                profiles.agent,
                name="n",
                network_name="net",
                labels={},
                volumes={"/srv/artifact-lib": "toolkit-1"},
            )

    @pytest.mark.parametrize("source", ["/var/run/docker.sock", "./rel", "a/b", "/host/path"])
    def test_a_host_path_is_refused_as_a_volume_source(
        self, profiles: ContainerProfiles, source: str
    ) -> None:
        with pytest.raises(ValueError, match="named volume"):
            build_create_args(
                profiles.agent,
                name="n",
                network_name="net",
                labels={},
                volumes={"/workspace": source, "/srv/artifact-lib": "toolkit-1"},
            )


class TestModelIsStrict:
    """The spec is immutable data and rejects unknown keys."""

    def test_profile_is_frozen(self, profiles: ContainerProfiles) -> None:
        with pytest.raises(ValidationError):
            profiles.agent.memory_mb = 1  # type: ignore[misc]

    def test_unknown_key_is_rejected(self, profiles: ContainerProfiles) -> None:
        data = profiles.agent.model_dump() | {"privileged": True}
        with pytest.raises(ValidationError):
            ContainerProfile.model_validate(data)

    def test_opencode_pin_is_a_released_2x_version(self, profiles: ContainerProfiles) -> None:
        assert profiles.opencode_version.startswith("2.")


class TestOpenCodePinMatchesTheDockerfile:
    """The agent image and the spec name the same OpenCode version."""

    def test_dockerfile_arg_equals_the_spec(self, profiles: ContainerProfiles) -> None:
        dockerfile = (
            Path(__file__).resolve().parents[3] / "docker" / "dev-agent" / "Dockerfile.browser"
        )
        match = re.search(r"^ARG OPENCODE_VERSION=(\S+)$", dockerfile.read_text(), re.MULTILINE)
        assert match is not None
        assert match.group(1) == profiles.opencode_version
