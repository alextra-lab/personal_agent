"""The launcher spec of the dev-agent containers, as data (ADR-0159 D3.3, D7.3, D8.8).

The spec lives in ``config/dev_agent/container_profiles.yaml``. This module holds the frozen
model that validates it and :func:`build_create_args`, which renders a profile into the
arguments of ``docker create``. The pattern is the ``run_python`` launcher in
``tools/primitives/sandbox.py``: an explicit argument list, never a shell.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict

#: The committed spec, resolved from the package and not from the working directory.
DEFAULT_PROFILES_PATH: Path = (
    Path(__file__).resolve().parents[3] / "config" / "dev_agent" / "container_profiles.yaml"
)

#: A named Docker volume. It has no slash, so it cannot be a host path or the Docker socket.
_VOLUME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class _Frozen(BaseModel):
    """Base for the spec models: immutable, and unknown keys are an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class TmpfsMount(_Frozen):
    """A writable in-memory path of a container.

    Attributes:
        path: Mount point in the container.
        size_mb: Size cap in megabytes. Tmpfs memory counts against the container memory limit.
        mode: Octal permission string of the mount point.
        exec: Whether files on the mount can be executed.
    """

    path: str
    size_mb: int
    mode: str
    exec: bool


class VolumeMount(_Frozen):
    """A named-volume mount. The launcher supplies the volume name.

    Attributes:
        target: Mount point in the container.
        read_only: Whether the container sees the volume read-only.
    """

    target: str
    read_only: bool


class ContainerProfile(_Frozen):
    """The boundary of one kind of container.

    Attributes:
        name: Profile name.
        image: Image tag.
        user: ``uid:gid`` of the process. Never root.
        network: ``dev-agent`` for the internal dev-agent network, ``none`` for no network.
        memory_mb: Memory cap. Swap is set to the same value, so the cap is hard.
        cpus: CPU quota.
        pids_limit: Process limit.
        read_only_root: Whether the root filesystem is read-only.
        cap_drop: Linux capabilities to drop.
        no_new_privileges: Whether privilege gain is blocked.
        init: Whether Docker runs a minimal init process (reaps browser children).
        tmpfs: The in-memory writable paths.
        mounts: The volume mounts. No other mount exists.
        env_fixed: The whole non-secret environment, with values.
        env_from_launcher: Names of variables whose value the launcher passes at start.
        wall_clock_seconds: Time limit that the supervisor enforces. Docker does not.
    """

    name: Literal["agent", "check", "handoff"]
    image: str
    user: str
    network: Literal["dev-agent", "none"]
    memory_mb: int
    cpus: float
    pids_limit: int
    read_only_root: bool
    cap_drop: tuple[str, ...]
    no_new_privileges: bool
    init: bool
    tmpfs: tuple[TmpfsMount, ...]
    mounts: tuple[VolumeMount, ...]
    env_fixed: Mapping[str, str]
    env_from_launcher: tuple[str, ...]
    wall_clock_seconds: int


class ContainerProfiles(_Frozen):
    """The whole spec file.

    Attributes:
        opencode_version: The one pinned OpenCode version of the agent image.
        agent: The agent container.
        check: The publish-check container.
        handoff: The handoff container.
    """

    opencode_version: str
    agent: ContainerProfile
    check: ContainerProfile
    handoff: ContainerProfile


def load_profiles(path: Path = DEFAULT_PROFILES_PATH) -> ContainerProfiles:
    """Load and validate the spec.

    Args:
        path: Spec file. Defaults to the committed one.

    Returns:
        The validated, immutable spec.

    Raises:
        pydantic.ValidationError: If the file does not match the model.
        OSError: If the file cannot be read.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ContainerProfiles.model_validate(raw)


def tmpfs_option(mount: TmpfsMount, user: str) -> str:
    """Render ``--tmpfs`` options. The mount belongs to the container user."""
    uid, _, gid = user.partition(":")
    exec_option = "exec" if mount.exec else "noexec"
    return (
        f"{mount.path}:rw,nosuid,nodev,{exec_option},size={mount.size_mb}m,"
        f"mode={mount.mode},uid={uid},gid={gid}"
    )


def build_create_args(
    profile: ContainerProfile,
    *,
    name: str,
    network_name: str,
    labels: Mapping[str, str],
    volumes: Mapping[str, str],
) -> list[str]:
    """Render a profile into the arguments of ``docker create``.

    Args:
        profile: The container profile.
        name: Container name.
        network_name: Real name of the ``dev-agent`` network. Ignored when the profile has no network.
        labels: Labels to put on the container (for example the job id).
        volumes: Named volume for each mount target of the profile.

    Returns:
        The argument list, starting with ``docker create`` and ending with the image.

    Raises:
        ValueError: If a mount target has no volume, or a volume is not a plain volume name.
    """
    args: list[str] = [
        "docker",
        "create",
        "--name",
        name,
        "--memory",
        f"{profile.memory_mb}m",
        "--memory-swap",
        f"{profile.memory_mb}m",
        "--cpus",
        str(profile.cpus),
        "--pids-limit",
        str(profile.pids_limit),
        "--network",
        network_name if profile.network == "dev-agent" else "none",
        "-u",
        profile.user,
    ]
    for key, value in labels.items():
        args += ["--label", f"{key}={value}"]
    if profile.read_only_root:
        args.append("--read-only")
    if profile.init:
        args.append("--init")
    for capability in profile.cap_drop:
        args += ["--cap-drop", capability]
    if profile.no_new_privileges:
        args += ["--security-opt", "no-new-privileges"]
    for tmpfs in profile.tmpfs:
        args += ["--tmpfs", tmpfs_option(tmpfs, profile.user)]
    for mount in profile.mounts:
        source = volumes.get(mount.target)
        if source is None:
            raise ValueError(f"no volume for mount target {mount.target}")
        if not _VOLUME_NAME.match(source):
            raise ValueError(f"{source!r} is not a named volume (mount target {mount.target})")
        args += ["-v", f"{source}:{mount.target}{':ro' if mount.read_only else ''}"]
    for env_name, env_value in profile.env_fixed.items():
        args += ["-e", f"{env_name}={env_value}"]
    for env_name in profile.env_from_launcher:
        # No value: Docker takes it from the launcher's own environment, so it never shows in `ps`.
        args += ["-e", env_name]
    args.append(profile.image)
    return args
