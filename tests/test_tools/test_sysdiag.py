"""Unit tests for run_sysdiag native tool (FRE-188).

Most tests mock asyncio.create_subprocess_exec so no real processes are
spawned. A small set of integration tests (marked @pytest.mark.integration)
run real commands against the host — deselect with -m "not integration".
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.sysdiag import (
    _ALLOW_LIST,
    run_sysdiag_executor,
    run_sysdiag_tool,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _mock_proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    """Build a mock asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(
        return_value=(stdout.encode(), stderr.encode())
    )
    proc.kill = MagicMock()
    return proc


# ── Tool definition tests ──────────────────────────────────────────────────


def test_tool_definition() -> None:
    """Tool has correct metadata."""
    assert run_sysdiag_tool.name == "run_sysdiag"
    assert run_sysdiag_tool.category == "read_only"
    assert run_sysdiag_tool.risk_level == "low"
    assert "NORMAL" in run_sysdiag_tool.allowed_modes
    assert "ALERT" in run_sysdiag_tool.allowed_modes
    assert "DEGRADED" in run_sysdiag_tool.allowed_modes
    assert "LOCKDOWN" not in run_sysdiag_tool.allowed_modes
    param_names = {p.name for p in run_sysdiag_tool.parameters}
    assert {"command", "args", "timeout"} <= param_names


def test_allow_list_not_empty() -> None:
    """Allow-list contains the expected commands."""
    expected = {"ps", "lsof", "find", "df", "du", "iostat", "vm_stat",
                "ifconfig", "netstat", "pgrep", "top", "uptime", "sysctl",
                "who", "last", "sw_vers", "diskutil"}
    assert expected <= set(_ALLOW_LIST)


# ── Validation tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_command_raises() -> None:
    with pytest.raises(ToolExecutionError, match="not available on this platform"):
        await run_sysdiag_executor(command="")


@pytest.mark.asyncio
async def test_disallowed_command_raises() -> None:
    with pytest.raises(ToolExecutionError, match="not available on this platform"):
        await run_sysdiag_executor(command="rm")


@pytest.mark.asyncio
async def test_shell_injection_attempt_is_blocked() -> None:
    """Shell metacharacters in command name are rejected, not executed."""
    with pytest.raises(ToolExecutionError, match="not available on this platform"):
        await run_sysdiag_executor(command="ps; rm -rf /")


@pytest.mark.asyncio
async def test_invalid_args_quoting_raises() -> None:
    """Malformed shell quoting in args raises ToolExecutionError."""
    proc = _mock_proc("output")
    with patch("personal_agent.tools.sysdiag.asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(ToolExecutionError, match="Cannot parse args"):
            await run_sysdiag_executor(command="ps", args="'unclosed quote")


# ── Success path tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_basic_command_success() -> None:
    """Successful command returns stdout, stderr, exit_code, command_used."""
    proc = _mock_proc(stdout="PID  CMD\n1234 python\n", returncode=0)
    with patch(
        "personal_agent.tools.sysdiag.asyncio.create_subprocess_exec",
        return_value=proc,
    ) as mock_exec:
        result = await run_sysdiag_executor(command="ps", args="aux")

    assert result["exit_code"] == 0
    assert "python" in result["stdout"]
    assert result["stderr"] == ""
    assert result["truncated"] is False
    assert "ps" in result["command_used"]
    # Verify no shell=True was used
    mock_exec.assert_called_once()
    call_kwargs = mock_exec.call_args.kwargs
    assert "shell" not in call_kwargs or call_kwargs.get("shell") is not True


@pytest.mark.asyncio
async def test_command_with_no_args() -> None:
    """Command can be run with no args."""
    proc = _mock_proc(stdout="uptime output\n", returncode=0)
    with patch("personal_agent.tools.sysdiag.asyncio.create_subprocess_exec", return_value=proc):
        result = await run_sysdiag_executor(command="uptime")

    assert result["exit_code"] == 0
    assert result["command_used"] == "/usr/bin/uptime"


@pytest.mark.asyncio
async def test_nonzero_exit_code_returned() -> None:
    """Non-zero exit code is returned as-is, not raised as an error."""
    proc = _mock_proc(stdout="", stderr="no matches", returncode=1)
    with patch("personal_agent.tools.sysdiag.asyncio.create_subprocess_exec", return_value=proc):
        result = await run_sysdiag_executor(command="pgrep", args="nonexistent_process_xyz")

    assert result["exit_code"] == 1
    assert "no matches" in result["stderr"]


@pytest.mark.asyncio
async def test_output_truncation() -> None:
    """Output longer than 32,000 chars is truncated."""
    large_output = "x" * 40_000
    proc = _mock_proc(stdout=large_output, returncode=0)
    with patch("personal_agent.tools.sysdiag.asyncio.create_subprocess_exec", return_value=proc):
        result = await run_sysdiag_executor(command="find", args="/")

    assert result["truncated"] is True
    assert len(result["stdout"]) <= 32_100  # 32000 + "[... truncated]"
    assert "[... truncated]" in result["stdout"]


@pytest.mark.asyncio
async def test_timeout_kills_process() -> None:
    """Process that exceeds timeout is killed and raises ToolExecutionError.

    asyncio.wait_for is mocked to raise TimeoutError; proc.communicate()
    returns empty bytes on the drain call (after kill).
    """
    proc = MagicMock()
    proc.returncode = None
    proc.kill = MagicMock()
    # communicate() drain call (after kill) returns empty bytes.
    proc.communicate = AsyncMock(return_value=(b"", b""))

    async def _timeout_wait_for(coro: object, timeout: float) -> object:
        raise asyncio.TimeoutError()

    with patch("personal_agent.tools.sysdiag.asyncio.create_subprocess_exec", return_value=proc):
        with patch("personal_agent.tools.sysdiag.asyncio.wait_for", new=_timeout_wait_for):
            with pytest.raises(ToolExecutionError, match="timed out"):
                await run_sysdiag_executor(command="find", args="/", timeout=1)

    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_timeout_capped_at_max() -> None:
    """Timeout is capped at _MAX_TIMEOUT regardless of what caller passes."""
    proc = _mock_proc(stdout="ok\n", returncode=0)
    with patch(
        "personal_agent.tools.sysdiag.asyncio.create_subprocess_exec", return_value=proc
    ):
        with patch("personal_agent.tools.sysdiag.asyncio.wait_for", new_callable=AsyncMock) as mock_wf:
            mock_wf.return_value = (b"ok\n", b"")
            await run_sysdiag_executor(command="uptime", timeout=9999)
            _, kwargs = mock_wf.call_args
            assert kwargs.get("timeout", 9999) <= 60


@pytest.mark.asyncio
async def test_os_error_raises() -> None:
    """OS-level error (e.g. binary not found) raises ToolExecutionError."""
    with patch(
        "personal_agent.tools.sysdiag.asyncio.create_subprocess_exec",
        side_effect=OSError("No such file"),
    ):
        with pytest.raises(ToolExecutionError, match="OS error"):
            await run_sysdiag_executor(command="ps")


@pytest.mark.asyncio
async def test_args_parsed_as_list() -> None:
    """Multi-word args are split correctly and passed as separate argv entries."""
    proc = _mock_proc(stdout="result\n", returncode=0)
    with patch(
        "personal_agent.tools.sysdiag.asyncio.create_subprocess_exec", return_value=proc
    ) as mock_exec:
        with patch("personal_agent.tools.sysdiag.asyncio.wait_for", new_callable=AsyncMock) as mock_wf:
            mock_wf.return_value = (b"result\n", b"")
            await run_sysdiag_executor(command="lsof", args="-i :9000 -n")

    call_args = mock_exec.call_args.args
    # First positional arg is the binary; rest are the parsed argv
    assert "/usr/sbin/lsof" in call_args
    assert "-i" in call_args
    assert ":9000" in call_args
    assert "-n" in call_args


# ── Integration tests (real commands, require host) ────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ps_real() -> None:
    """ps aux returns at least one process."""
    result = await run_sysdiag_executor(command="ps", args="aux")
    assert result["exit_code"] == 0
    assert len(result["stdout"]) > 0
    assert "PID" in result["stdout"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_df_real() -> None:
    """df -h returns disk info."""
    result = await run_sysdiag_executor(command="df", args="-h")
    assert result["exit_code"] == 0
    assert "Filesystem" in result["stdout"] or "filesystem" in result["stdout"].lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sw_vers_real() -> None:
    """sw_vers returns macOS version info."""
    result = await run_sysdiag_executor(command="sw_vers")
    assert result["exit_code"] == 0
    assert "macOS" in result["stdout"] or "ProductName" in result["stdout"]
