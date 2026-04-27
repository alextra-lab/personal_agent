"""Docker-based sandbox execution layer for primitive tools.

Provides :func:`run_in_sandbox` which spawns a Docker container via the
``docker`` CLI (subprocess) to execute arbitrary code in a hardened, isolated
environment.

Security model
--------------
* Non-root user (uid/gid 1000) inside the container.
* Read-only root filesystem (``--read-only``).
* ``/tmp`` mounted as a tmpfs (64 MB) for in-container temporary storage.
* ``/sandbox`` bind-mounted from a per-trace host scratch directory (rw).
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

FRE-261 Step 5.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from personal_agent.telemetry import get_logger

log = get_logger(__name__)


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
        scratch_files: List of absolute host paths of files present in the
            scratch directory after the run (files written by the script).
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
) -> SandboxResult:
    """Run *script* inside a Docker container with hard security constraints.

    Args:
        image: Docker image name (e.g. ``seshat-sandbox-python:0.1``).
        script: Python source code string to pass as ``python -c <script>``.
        timeout_seconds: Maximum seconds to wait for the container to finish.
            The outer asyncio timeout is ``timeout_seconds + 5`` to give Docker
            a chance to surface its own error before the outer kill fires.
        memory_mb: Container memory limit in megabytes (default 512).
        cpus: CPU quota as a fraction (default 1.0).
        network: When True attach to the ``cloud-sim`` Docker network; when
            False pass ``--network=none`` (no outbound access).
        scratch_host_path: Absolute path on the Docker host that will be
            bind-mounted as ``/sandbox`` inside the container (rw).  Created
            automatically if it does not yet exist.

    Returns:
        :class:`SandboxResult` describing the outcome.  Never raises;
        errors are surfaced through the ``SandboxResult`` fields.
    """
    if shutil.which("docker") is None:
        log.warning("sandbox_unavailable", reason="docker binary not found")
        return _DOCKER_UNAVAILABLE

    # Ensure scratch directory exists on the host before Docker tries to mount it.
    try:
        scratch_host_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error("sandbox_scratch_mkdir_failed", path=str(scratch_host_path), error=str(exc))
        return SandboxResult(
            exit_code=1,
            stdout="",
            stderr=f"Failed to create sandbox scratch dir: {exc}",
            oom=False,
            timed_out=False,
            scratch_files=[],
        )

    network_arg = "cloud-sim" if network else "none"

    # Build the docker run command as an explicit argument list (no shell=True).
    # Each element is a separate string — no shell interpolation possible.
    docker_args: list[str] = [
        "docker",
        "run",
        "--rm",
        f"--memory={memory_mb}m",
        f"--cpus={cpus}",
        f"--network={network_arg}",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=64m",
        "-v",
        f"{scratch_host_path}:/sandbox:rw",
        "-u",
        "1000:1000",
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
    )

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *docker_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=float(timeout_seconds) + 5,
        )
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except OSError:
                pass
        log.warning("sandbox_timed_out", image=image, timeout_seconds=timeout_seconds)
        return SandboxResult(
            exit_code=1,
            stdout="",
            stderr="",
            oom=False,
            timed_out=True,
            scratch_files=_list_scratch_files(scratch_host_path),
        )
    except OSError as exc:
        log.error("sandbox_os_error", image=image, error=str(exc))
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

    scratch_files = _list_scratch_files(scratch_host_path)

    log.info(
        "sandbox_finished",
        image=image,
        exit_code=exit_code,
        oom=oom,
        stdout_len=len(stdout_str),
        stderr_len=len(stderr_str),
        scratch_files_count=len(scratch_files),
    )

    return SandboxResult(
        exit_code=exit_code,
        stdout=stdout_str,
        stderr=stderr_str,
        oom=oom,
        timed_out=False,
        scratch_files=scratch_files,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_scratch_files(path: Path) -> list[str]:
    """Return a list of absolute paths of files in *path* (non-recursive).

    Args:
        path: Directory to list.

    Returns:
        Sorted list of string paths, or an empty list if *path* does not exist
        or cannot be listed.
    """
    try:
        if not path.exists():
            return []
        return sorted(str(f) for f in path.iterdir() if f.is_file())
    except OSError:
        return []
