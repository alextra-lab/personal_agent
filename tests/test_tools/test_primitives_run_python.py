"""Unit tests for the run_python primitive tool (tools/primitives/run_python.py).

All external calls are mocked — no Docker containers or real processes are used.

FRE-261 Step 5.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.tools.primitives.run_python import (
    _DEFAULT_TIMEOUT,
    _MAX_TIMEOUT,
    _MIN_TIMEOUT,
    run_python_executor,
)
from personal_agent.tools.primitives.sandbox import SandboxResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _success_result(stdout: str = "", stderr: str = "") -> SandboxResult:
    return SandboxResult(
        exit_code=0,
        stdout=stdout,
        stderr=stderr,
        oom=False,
        timed_out=False,
        scratch_files=[],
    )


def _failure_result(
    exit_code: int = 1,
    stdout: str = "",
    stderr: str = "error",
    oom: bool = False,
    timed_out: bool = False,
) -> SandboxResult:
    return SandboxResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        oom=oom,
        timed_out=timed_out,
        scratch_files=[],
    )


# ---------------------------------------------------------------------------
# Pre-flight guard tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_python_no_docker() -> None:
    """When docker binary is absent, returns sandbox_unavailable error dict."""
    with patch("shutil.which", return_value=None):
        result = await run_python_executor(script="print('hello')")

    assert result["success"] is False
    assert result["error"] == "sandbox_unavailable"
    assert "docker" in result["detail"].lower()


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_python_happy_path() -> None:
    """Successful execution returns success=True and stdout."""
    sandbox_result = _success_result(stdout="42\n")

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "personal_agent.tools.primitives.run_python.run_in_sandbox",
            new=AsyncMock(return_value=sandbox_result),
        ),
    ):
        result = await run_python_executor(script="print(42)")

    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "42\n"
    assert result["timed_out"] is False
    assert result["oom"] is False
    assert result["truncated"] is False


# ---------------------------------------------------------------------------
# Timeout clamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_python_timeout_clamp_high() -> None:
    """timeout_seconds=9999 is clamped to MAX_TIMEOUT."""
    captured_kwargs: dict = {}

    async def capture_sandbox(**kwargs: object) -> SandboxResult:
        captured_kwargs.update(kwargs)
        return _success_result()

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "personal_agent.tools.primitives.run_python.run_in_sandbox",
            new=capture_sandbox,
        ),
    ):
        await run_python_executor(script="pass", timeout_seconds=9999)

    assert captured_kwargs["timeout_seconds"] == _MAX_TIMEOUT


@pytest.mark.asyncio
async def test_run_python_timeout_clamp_low() -> None:
    """timeout_seconds=0 is clamped to MIN_TIMEOUT."""
    captured_kwargs: dict = {}

    async def capture_sandbox(**kwargs: object) -> SandboxResult:
        captured_kwargs.update(kwargs)
        return _success_result()

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "personal_agent.tools.primitives.run_python.run_in_sandbox",
            new=capture_sandbox,
        ),
    ):
        await run_python_executor(script="pass", timeout_seconds=0)

    assert captured_kwargs["timeout_seconds"] == _MIN_TIMEOUT


# ---------------------------------------------------------------------------
# Timeout and OOM propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_python_timeout_propagated() -> None:
    """timed_out=True from sandbox is surfaced in the executor result."""
    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "personal_agent.tools.primitives.run_python.run_in_sandbox",
            new=AsyncMock(return_value=_failure_result(timed_out=True)),
        ),
    ):
        result = await run_python_executor(script="import time; time.sleep(999)")

    assert result["timed_out"] is True
    assert result["success"] is False


@pytest.mark.asyncio
async def test_run_python_oom_propagated() -> None:
    """oom=True from sandbox is surfaced in the executor result."""
    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "personal_agent.tools.primitives.run_python.run_in_sandbox",
            new=AsyncMock(return_value=_failure_result(exit_code=137, oom=True)),
        ),
    ):
        result = await run_python_executor(script="x = bytearray(2**30)")

    assert result["oom"] is True
    assert result["success"] is False


# ---------------------------------------------------------------------------
# Output truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_python_output_truncation() -> None:
    """Output exceeding 50 KiB is truncated and truncated=True is set."""
    big_stdout = "x" * 60_000  # > 50 KiB

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "personal_agent.tools.primitives.run_python.run_in_sandbox",
            new=AsyncMock(return_value=_success_result(stdout=big_stdout)),
        ),
    ):
        result = await run_python_executor(script="print('x' * 60000)")

    assert result["truncated"] is True
    # stdout should be capped at ~25 KiB
    assert len(result["stdout"].encode("utf-8")) <= 25_601


# ---------------------------------------------------------------------------
# Scratch files propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_python_scratch_files_propagated(tmp_path: Path) -> None:
    """scratch_files from sandbox result are returned in executor result."""
    fake_files = ["/tmp/agent_sandbox/test_trace/output.txt"]
    sandbox_result = SandboxResult(
        exit_code=0,
        stdout="done\n",
        stderr="",
        oom=False,
        timed_out=False,
        scratch_files=fake_files,
    )

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "personal_agent.tools.primitives.run_python.run_in_sandbox",
            new=AsyncMock(return_value=sandbox_result),
        ),
    ):
        result = await run_python_executor(script="open('/sandbox/output.txt','w').write('x')")

    assert result["scratch_files"] == fake_files


# ---------------------------------------------------------------------------
# Network flag forwarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_python_network_flag_forwarded() -> None:
    """network=True is forwarded to run_in_sandbox."""
    captured_kwargs: dict = {}

    async def capture_sandbox(**kwargs: object) -> SandboxResult:
        captured_kwargs.update(kwargs)
        return _success_result()

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "personal_agent.tools.primitives.run_python.run_in_sandbox",
            new=capture_sandbox,
        ),
    ):
        await run_python_executor(script="pass", network=True)

    assert captured_kwargs["network"] is True


# ---------------------------------------------------------------------------
# Integration test (skipped in unit mode)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_python_integration() -> None:
    """Actually spawn a Docker container to run a trivial script.

    Requires:
    - PERSONAL_AGENT_INTEGRATION=1
    - docker info succeeds
    - seshat-sandbox-python:0.1 image built (make sandbox-build)
    """
    import os
    import subprocess

    if os.environ.get("PERSONAL_AGENT_INTEGRATION") != "1":
        pytest.skip("PERSONAL_AGENT_INTEGRATION=1 not set")

    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("docker info failed — Docker not available or not running")

    result = await run_python_executor(script="print(6 * 7)")

    assert result["success"] is True
    assert "42" in result["stdout"]
    assert result["timed_out"] is False
    assert result["oom"] is False
