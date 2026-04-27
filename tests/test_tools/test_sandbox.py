"""Unit tests for the sandbox execution layer (tools/primitives/sandbox.py).

All Docker subprocess calls are mocked — no real containers are spawned.

FRE-261 Step 5.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.tools.primitives.sandbox import SandboxResult, run_in_sandbox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_proc(
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> MagicMock:
    """Return a mock asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=None)
    return proc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_docker_not_available(tmp_path: Path) -> None:
    """When docker binary is absent, returns a graceful SandboxResult."""
    with patch("shutil.which", return_value=None):
        result = await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="print('hello')",
            timeout_seconds=10,
            scratch_host_path=tmp_path / "scratch",
        )

    assert result.exit_code == 1
    assert result.timed_out is False
    assert result.oom is False
    assert "docker" in result.stderr.lower()
    assert result.scratch_files == []


@pytest.mark.asyncio
async def test_sandbox_happy_path(tmp_path: Path) -> None:
    """Successful run returns stdout and exit_code=0."""
    scratch = tmp_path / "scratch"
    mock_proc = _make_mock_proc(returncode=0, stdout=b"42\n", stderr=b"")

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ),
    ):
        result = await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="print(42)",
            timeout_seconds=10,
            scratch_host_path=scratch,
        )

    assert result.exit_code == 0
    assert result.stdout == "42\n"
    assert result.stderr == ""
    assert result.timed_out is False
    assert result.oom is False


@pytest.mark.asyncio
async def test_sandbox_timeout(tmp_path: Path) -> None:
    """asyncio.TimeoutError → timed_out=True in result."""
    scratch = tmp_path / "scratch"
    mock_proc = _make_mock_proc(returncode=0)
    # Make communicate raise TimeoutError
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ),
        patch("asyncio.wait_for", side_effect=asyncio.TimeoutError),
    ):
        result = await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="import time; time.sleep(999)",
            timeout_seconds=1,
            scratch_host_path=scratch,
        )

    assert result.timed_out is True
    assert result.exit_code == 1
    assert result.oom is False


@pytest.mark.asyncio
async def test_sandbox_oom(tmp_path: Path) -> None:
    """Exit code 137 → oom=True in result."""
    scratch = tmp_path / "scratch"
    mock_proc = _make_mock_proc(returncode=137, stdout=b"", stderr=b"Killed\n")

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ),
    ):
        result = await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="x = bytearray(1024**3)",
            timeout_seconds=30,
            scratch_host_path=scratch,
        )

    assert result.oom is True
    assert result.exit_code == 137
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_sandbox_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero exit that is not 137 → oom=False, success=False."""
    scratch = tmp_path / "scratch"
    mock_proc = _make_mock_proc(returncode=1, stdout=b"", stderr=b"SyntaxError\n")

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ),
    ):
        result = await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="invalid python !!",
            timeout_seconds=10,
            scratch_host_path=scratch,
        )

    assert result.exit_code == 1
    assert result.oom is False
    assert result.timed_out is False
    assert "SyntaxError" in result.stderr


@pytest.mark.asyncio
async def test_sandbox_scratch_files_listed(tmp_path: Path) -> None:
    """Files created in scratch dir are reported in scratch_files."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    # Pre-create a file as if the script wrote it
    (scratch / "output.txt").write_text("result")

    mock_proc = _make_mock_proc(returncode=0, stdout=b"done\n")

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ),
    ):
        result = await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="open('/sandbox/output.txt', 'w').write('result')",
            timeout_seconds=10,
            scratch_host_path=scratch,
        )

    assert str(scratch / "output.txt") in result.scratch_files
