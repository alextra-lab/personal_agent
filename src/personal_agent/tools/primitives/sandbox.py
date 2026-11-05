"""Docker-based sandbox execution layer for primitive tools.

Provides :func:`run_in_sandbox` which spawns a Docker container via the
``docker`` CLI (subprocess) to execute arbitrary code in a hardened, isolated
environment.

Security model
--------------
* Non-root user (uid/gid 1000) inside the container.
* Read-only root filesystem (``--read-only``).
* ``/tmp`` mounted as a tmpfs (64 MB) for in-container temporary storage.
* ``/sandbox`` mounted (rw) from a per-trace scratch directory. A gateway that talks to the host
  daemon names a Docker volume (``sandbox_scratch_volume``), so the daemon and the gateway use
  the same directory. Otherwise the directory is bind-mounted by host path (FRE-1736).
* Network disabled by default (``--network=none``); opt-in via ``network=True``.
* All Linux capabilities dropped (``--cap-drop=ALL``).
* Privilege escalation blocked (``--security-opt=no-new-privileges``).

Subprocess approach
-------------------
Uses ``asyncio.create_subprocess_exec`` (never ``shell=True``) to call the
``docker`` binary directly, consistent with the existing ``run_sysdiag`` tool
pattern. Each argument is a separate list element, preventing shell injection.

Graceful fallback
-----------------
If the ``docker`` binary is not found on ``PATH`` the function returns a
``SandboxResult`` with ``exit_code=1`` and an explanatory ``stderr`` message
rather than raising an exception.

Container lifecycle (FRE-1462)
-------------------------------
Every container is given a unique ``--name`` and labels (tool, trace_id,
a hard removal deadline). ``docker run`` without ``-d`` kills only the local
CLI client on ``proc.kill()`` — the daemon-side container does not stop
just because its client disconnected, and ``--rm`` only fires on the
container's *own* exit. So cleanup always force-removes the named container
by ID in a ``finally`` block, on every exit path (success, timeout, error,
cancellation). :func:`run_sandbox_reaper` is the periodic backstop for the
process/event-loop-death case that a ``finally`` cannot cover.

FRE-261 Step 5. FRE-1462 (execution bound + cleanup).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from personal_agent.config import settings
from personal_agent.telemetry import get_logger
from personal_agent.tools.primitives._child_workspace import WORKSPACE_ROOT, path_for_root_open
from personal_agent.tools.primitives._governance import (
    make_directory_no_symlinks,
    open_no_symlinks,
)

log = get_logger(__name__)

# Grace window (seconds) added past a container's own timeout_seconds before
# the periodic reaper considers it an orphan. Must comfortably exceed the
# in-band cleanup path's own duration (local-proc kill/wait + docker rm -f)
# so the reaper does not race a cleanup that is already in flight.
_REAPER_GRACE_SECONDS = 45.0

# The account that the sandbox runs as (the ``-u`` flag), and that owns its scratch directory.
_SANDBOX_UID = 1000
_SANDBOX_GID = 1000

# Docker's rule for a volume name, and the rule for one part of a path inside the volume.
_VOLUME_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_SUBPATH_PART = re.compile(r"[A-Za-z0-9_.-]+")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of a sandbox execution.

    Attributes:
        exit_code: Process exit code (0 = success).
        stdout: Captured standard output (UTF-8, errors replaced).
        stderr: Captured standard error (UTF-8, errors replaced).
        oom: True when the container was killed by the OOM killer (exit 137).
        timed_out: True when the outer asyncio timeout fired before the
            container finished.
        scratch_files: List of absolute paths, as the calling process sees them, of the
            files in the scratch directory after the run (files written by the script).
    """

    exit_code: int
    stdout: str
    stderr: str
    oom: bool
    timed_out: bool
    scratch_files: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Sentinel result — docker binary not available
# ---------------------------------------------------------------------------

_DOCKER_UNAVAILABLE = SandboxResult(
    exit_code=1,
    stdout="",
    stderr="docker binary not found on PATH — sandbox unavailable",
    oom=False,
    timed_out=False,
    scratch_files=[],
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_in_sandbox(
    *,
    image: str,
    script: str,
    timeout_seconds: int,
    memory_mb: int = 512,
    cpus: float = 1.0,
    network: bool = False,
    scratch_host_path: Path,
    trace_id: str | None = None,
    tool: str = "unknown",
) -> SandboxResult:
    """Run *script* inside a Docker container with hard security constraints.

    Args:
        image: Docker image name (e.g. ``seshat-sandbox-python:0.1``).
        script: Python source code string to pass as ``python -c <script>``.
        timeout_seconds: Maximum seconds to wait for the container to finish.
            The container is force-removed as soon as this fires — the bound
            is the advertised limit, not the limit plus extra slack.
        memory_mb: Container memory limit in megabytes (default 512).
        cpus: CPU quota as a fraction (default 1.0).
        network: When True attach to the configured ``sandbox_network`` Docker
            network (from settings); when False pass ``--network=none`` (no outbound access).
        scratch_host_path: Absolute path, as this process sees it, of the directory that
            becomes ``/sandbox`` inside the container (rw). Created automatically if it does
            not yet exist. With ``settings.sandbox_scratch_volume`` set, the directory must be
            inside ``/app/agent_workspace``, where the gateway mounts that volume, and the
            daemon mounts the same directory by volume and subpath. With no volume set, the
            daemon mounts it by host path, so the host and this process must share the path.
        trace_id: Originating request trace_id, threaded onto every sandbox
            lifecycle log for §I3 identity threading. Defaults to ``None`` for
            callers without a request context (e.g. test fixtures).
        tool: Name of the calling tool (e.g. ``"run_python"``), stamped onto
            the container's labels and every lifecycle log so a terminated or
            reaped execution is attributable without shell access (FRE-1462
            AC-4).

    Returns:
        :class:`SandboxResult` describing the outcome. Never raises except
        ``asyncio.CancelledError``, which propagates after cleanup completes
        (it must — swallowing it would break upstream cancellation); every
        other error is surfaced through the ``SandboxResult`` fields
        instead. The container started by this call is force-removed
        before returning, or before the ``CancelledError`` propagates.
    """
    if shutil.which("docker") is None:
        log.warning("sandbox_unavailable", reason="docker binary not found", trace_id=trace_id)
        return _DOCKER_UNAVAILABLE

    # Name the mount before any side effect: a bad setting must not leave a directory behind.
    try:
        mount_args = _scratch_mount_args(scratch_host_path, settings.sandbox_scratch_volume)
    except ValueError as exc:
        log.error(
            "sandbox_scratch_mount_invalid",
            path=str(scratch_host_path),
            error=str(exc),
            trace_id=trace_id,
        )
        return SandboxResult(
            exit_code=1,
            stdout="",
            stderr=f"Cannot mount the sandbox scratch dir: {exc}",
            oom=False,
            timed_out=False,
            scratch_files=[],
        )

    # Ensure scratch directory exists before Docker tries to mount it.
    try:
        # The directory and its parents stay with this process (root on a deployed gateway), and
        # only the trace directory goes to the sandbox user (FRE-1736). The ``bash`` child owns
        # ``/app/agent_workspace`` itself, so it can still rename ``sandbox``. This is not a
        # barrier. The daemon keeps the mount inside the volume, whatever the child does.
        scratch_for_root = path_for_root_open(scratch_host_path)
        make_directory_no_symlinks(scratch_for_root)
        _give_to_sandbox_user(scratch_for_root)
    except OSError as exc:
        log.error(
            "sandbox_scratch_mkdir_failed",
            path=str(scratch_host_path),
            error=str(exc),
            trace_id=trace_id,
        )
        return SandboxResult(
            exit_code=1,
            stdout="",
            stderr=f"Failed to create sandbox scratch dir: {exc}",
            oom=False,
            timed_out=False,
            scratch_files=[],
        )

    network_arg = settings.sandbox_network if network else "none"

    container_name = f"seshat-sbx-{uuid.uuid4().hex[:16]}"
    deadline_epoch = time.time() + timeout_seconds + _REAPER_GRACE_SECONDS

    # Build the docker run command as an explicit argument list (no shell=True).
    # Each element is a separate string — no shell interpolation possible.
    # --name + --label make the container addressable and attributable by ID,
    # independent of the local CLI client's own process lifetime (FRE-1462).
    docker_args: list[str] = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--label",
        "seshat.sandbox=true",
        "--label",
        f"seshat.sandbox.tool={tool}",
        "--label",
        f"seshat.sandbox.trace_id={trace_id or ''}",
        "--label",
        f"seshat.sandbox.deadline={deadline_epoch}",
        f"--memory={memory_mb}m",
        f"--memory-swap={memory_mb}m",
        f"--cpus={cpus}",
        f"--network={network_arg}",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=64m",
        *mount_args,
        "-u",
        f"{_SANDBOX_UID}:{_SANDBOX_GID}",
        "--security-opt",
        "no-new-privileges",
        "--cap-drop",
        "ALL",
        image,
        script,
    ]

    log.info(
        "sandbox_starting",
        image=image,
        timeout_seconds=timeout_seconds,
        memory_mb=memory_mb,
        network=network,
        scratch=str(scratch_host_path),
        trace_id=trace_id,
        tool=tool,
        container=container_name,
    )

    proc: asyncio.subprocess.Process | None = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(timeout_seconds),
            )
        except asyncio.TimeoutError:
            log.warning(
                "sandbox_timed_out",
                image=image,
                timeout_seconds=timeout_seconds,
                trace_id=trace_id,
                tool=tool,
                container=container_name,
            )
            return SandboxResult(
                exit_code=1,
                stdout="",
                stderr=(f"Execution exceeded the {timeout_seconds}s limit and was terminated."),
                oom=False,
                timed_out=True,
                scratch_files=_list_scratch_files(scratch_host_path),
            )
        except OSError as exc:
            log.error(
                "sandbox_os_error",
                image=image,
                error=str(exc),
                trace_id=trace_id,
                tool=tool,
                container=container_name,
            )
            return SandboxResult(
                exit_code=1,
                stdout="",
                stderr=str(exc),
                oom=False,
                timed_out=False,
                scratch_files=[],
            )

        exit_code: int = proc.returncode if proc.returncode is not None else -1
        stdout_str = stdout_bytes.decode("utf-8", errors="replace")
        stderr_str = stderr_bytes.decode("utf-8", errors="replace")

        # OOM kill: Docker sets exit code 137 (128 + SIGKILL) when the container
        # is killed by the kernel OOM killer due to memory exhaustion.
        oom = exit_code == 137

        # Detect network attachment failure (FRE-1466 AC-4): emit a named event
        # so the failure is queryable, not just paraphrased by the model into prose.
        if exit_code != 0 and network:
            if "network" in stderr_str.lower() and (
                "not found" in stderr_str.lower() or "not connected" in stderr_str.lower()
            ):
                log.warning(
                    "sandbox_network_attachment_failed",
                    image=image,
                    network=settings.sandbox_network,
                    trace_id=trace_id,
                    tool=tool,
                    container=container_name,
                )

        scratch_files = _list_scratch_files(scratch_host_path)

        log.info(
            "sandbox_finished",
            image=image,
            exit_code=exit_code,
            oom=oom,
            stdout_len=len(stdout_str),
            stderr_len=len(stderr_str),
            scratch_files_count=len(scratch_files),
            trace_id=trace_id,
            tool=tool,
            container=container_name,
        )

        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout_str,
            stderr=stderr_str,
            oom=oom,
            timed_out=False,
            scratch_files=scratch_files,
        )
    finally:
        # Runs on every exit path — success, timeout, OSError, and an
        # uncaught asyncio.CancelledError alike (Python runs `finally` while
        # unwinding cancellation). This is what closes FRE-1462: the previous
        # code only ever killed the local `docker run` CLI client, which does
        # not stop the daemon-side container — `--rm` fires on the
        # container's own exit, not on client disconnect. The periodic
        # reaper (`run_sandbox_reaper`) is the backstop for the residual gap
        # this can't cover: process/event-loop death before `finally` runs.
        await _cleanup_container(container_name, proc, trace_id=trace_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scratch_mount_args(scratch: Path, volume: str) -> list[str]:
    """Return the ``docker run`` arguments that mount *scratch* as ``/sandbox``.

    The gateway reaches the host daemon through a socket. The daemon resolves a ``-v`` source on
    the host, where the gateway's path names another directory (FRE-1736). With a volume set,
    the arguments name the volume and the subpath of *scratch* inside it, and the daemon reads
    the same directory as the gateway.

    Args:
        scratch: The scratch directory, as this process sees it.
        volume: The Docker volume that the gateway mounts at ``/app/agent_workspace``. Empty
            for a mount by host path.

    Returns:
        The mount arguments.

    Raises:
        ValueError: The volume name or the subpath is not safe in a ``--mount`` value, or
            *scratch* is outside the workspace.
    """
    if not volume:
        return ["-v", f"{scratch}:/sandbox:rw"]
    if not _VOLUME_NAME.fullmatch(volume):
        raise ValueError(f"sandbox_scratch_volume {volume!r} is not a Docker volume name")
    try:
        parts = scratch.relative_to(WORKSPACE_ROOT).parts
    except ValueError:
        raise ValueError(
            f"sandbox_scratch_volume is set, but {scratch} is outside {WORKSPACE_ROOT}"
        ) from None
    if not parts or any(not _SUBPATH_PART.fullmatch(part) or part in {".", ".."} for part in parts):
        raise ValueError(f"{scratch} is not a safe subpath of the volume {volume}")
    subpath = "/".join(parts)
    return ["--mount", f"type=volume,src={volume},dst=/sandbox,volume-subpath={subpath}"]


def _give_to_sandbox_user(path: Path) -> None:
    """Give one directory to the sandbox's account, when this process is root.

    The sandbox runs as uid 1000 and cannot write a directory that root or ``nobody`` owns. The
    directory opens with no symbolic link, and the change goes through that descriptor. It runs
    on every call, so a directory that exists already is given too.

    Args:
        path: The scratch directory, in the form that the no-link open needs.

    Raises:
        OSError: The directory cannot open or change. The sandbox could not write it.
    """
    if os.geteuid() != 0:
        return
    fd = open_no_symlinks(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK)
    try:
        os.fchown(fd, _SANDBOX_UID, _SANDBOX_GID)
    finally:
        os.close(fd)


def _list_scratch_files(path: Path) -> list[str]:
    """Return a list of absolute paths of files in *path* (non-recursive).

    This process is root and the ``bash`` child can own this directory, so the listing goes
    through a descriptor that opened with no symbolic link (FRE-1732). A link in the directory is
    not listed.

    Args:
        path: Directory to list.

    Returns:
        Sorted list of string paths, or an empty list if *path* does not exist
        or cannot be listed.
    """
    try:
        fd = open_no_symlinks(path_for_root_open(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            files: list[str] = []
            for name in os.listdir(fd):
                with suppress(FileNotFoundError):  # one removed file does not lose the others
                    if stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                        files.append(str(path / name))
            return sorted(files)
        finally:
            os.close(fd)
    except OSError:
        return []


async def _force_remove_container(name: str, *, trace_id: str | None) -> bool:
    """Force-remove a container by name or ID (``docker rm -f``).

    Idempotent: a container already gone (removed by ``--rm`` on its own
    exit, or by a concurrent cleanup/reaper pass) is treated as success, not
    an error.

    Args:
        name: Container name or ID.
        trace_id: Originating request trace_id for identity threading.

    Returns:
        True when the container is confirmed gone (removed by this call, or
        already absent). False when removal could not be confirmed — the
        caller must not treat this as a completed reap.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "-f",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except (asyncio.TimeoutError, OSError) as exc:
        log.error(
            "sandbox_container_force_remove_error",
            container=name,
            error=str(exc),
            trace_id=trace_id,
        )
        return False

    if proc.returncode == 0:
        log.info("sandbox_container_removed", container=name, trace_id=trace_id)
        return True

    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    if "No such container" in stderr_text:
        log.debug("sandbox_container_already_gone", container=name, trace_id=trace_id)
        return True

    log.warning(
        "sandbox_container_force_remove_failed",
        container=name,
        stderr=stderr_text,
        trace_id=trace_id,
    )
    return False


async def _cleanup_container(
    name: str,
    proc: asyncio.subprocess.Process | None,
    *,
    trace_id: str | None,
) -> None:
    """Ensure the local CLI client is dead, then force-remove its container.

    The local ``docker run`` client is killed first (bounded) so it cannot
    go on to attach a container under *name* after removal has already run —
    a narrow race that can otherwise still occur when cleanup is triggered
    very early (before the daemon has finished creating the container); the
    periodic reaper (:func:`run_sandbox_reaper`) is the backstop for that
    residual case, since a container created after this call still carries
    the same deadline label.

    Args:
        name: The container's ``--name``.
        proc: The local ``docker run`` CLI subprocess, if it was created.
        trace_id: Originating request trace_id for identity threading.
    """
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except OSError, asyncio.TimeoutError:
            pass
    await _force_remove_container(name, trace_id=trace_id)


async def reap_orphaned_sandbox_containers() -> int:
    """Force-remove sandbox containers whose deadline label has passed.

    Backstop for exit paths a ``finally`` block cannot cover — process or
    event-loop death between container creation and in-band cleanup. Sweeps
    every container labelled ``seshat.sandbox=true`` regardless of which
    process started it, so an orphan is reaped without depending on a later
    turn happening to run (FRE-1462 AC-3).

    Returns:
        Count of containers confirmed removed this sweep. A container whose
        removal could not be confirmed is logged but not counted — it stays
        labelled and is retried on the next sweep.
    """
    if shutil.which("docker") is None:
        return 0

    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "ps",
            "--filter",
            "label=seshat.sandbox=true",
            "--format",
            '{{.ID}}\t{{.Label "seshat.sandbox.deadline"}}\t{{.Label "seshat.sandbox.trace_id"}}'
            '\t{{.Label "seshat.sandbox.tool"}}',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=15.0)
    except (asyncio.TimeoutError, OSError) as exc:
        log.error("sandbox_reaper_ps_error", error=str(exc))
        return 0

    if proc.returncode != 0:
        log.warning(
            "sandbox_reaper_ps_failed",
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
        )
        return 0

    now = time.time()
    reaped = 0
    for line in stdout_bytes.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        container_id, deadline_str, container_trace_id, tool = parts
        try:
            deadline = float(deadline_str)
        except ValueError:
            continue
        if now < deadline:
            continue

        removed = await _force_remove_container(container_id, trace_id=container_trace_id or None)
        if removed:
            log.warning(
                "sandbox_reaper_orphan_reaped",
                container=container_id,
                trace_id=container_trace_id or None,
                tool=tool or "unknown",
                deadline=deadline,
                age_past_deadline_seconds=now - deadline,
            )
            reaped += 1
        else:
            log.warning(
                "sandbox_reaper_orphan_remove_failed",
                container=container_id,
                trace_id=container_trace_id or None,
                tool=tool or "unknown",
                deadline=deadline,
            )

    return reaped


async def run_sandbox_reaper(*, interval_seconds: float = 60.0) -> None:
    """Sweep orphaned sandbox containers on a fixed cadence until cancelled.

    Mirrors :func:`personal_agent.cost_gate.reaper.run_reaper`'s shape.

    Args:
        interval_seconds: Seconds between sweeps. Defaults to 60s, which
            paired with ``_REAPER_GRACE_SECONDS`` bounds how long an orphaned
            container can outlive its own timeout.

    Cancellation:
        Cancel the task at shutdown. The function suppresses
        ``asyncio.CancelledError`` and exits cleanly.
    """
    log.info("sandbox_reaper_started", interval_seconds=interval_seconds)
    try:
        while True:
            try:
                await reap_orphaned_sandbox_containers()
            except Exception as exc:  # noqa: BLE001 — log + continue is the right thing here
                log.error("sandbox_reaper_sweep_failed", error=str(exc), exc_info=True)
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        log.info("sandbox_reaper_stopped")
        with suppress(asyncio.CancelledError):
            raise
