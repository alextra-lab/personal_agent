"""Sandboxed bash command executor primitive tool.

Provides a ``bash`` tool that executes shell commands in the agent's container
with an allowlist-based approval model and hard-deny patterns for truly
dangerous commands.

Security model
--------------
* Hard-deny regex patterns are checked *before* any subprocess is spawned.
  Even if governance config is misconfigured, these patterns prevent the most
  catastrophic commands from executing.
* Commands are parsed via ``shlex.split`` -- never ``shell=True``.
* Timeout is clamped to [1, 120] seconds.
* Output is capped at 50 KiB (combined stdout + stderr); overflow is written to
  a scratch file and the path is returned.
* The auto_approve_prefixes list in tools.yaml controls which commands bypass
  the PWA approval prompt (evaluated by the governance / _check_permissions
  layer, not the executor itself).

FRE-261 Step 4.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from pathlib import Path
from typing import Any

from personal_agent.config import load_governance_config
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_OUTPUT_BYTES = 51_200  # 50 KiB
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120

# Fallback deny-patterns used when governance config is unavailable.
# Belt-and-suspenders: the authoritative list lives in tools.yaml.
_FALLBACK_DENY: list[str] = [
    r"\brm\s+-rf\b",
    r"\bdd\s+if=",
    r"\bmkfs\b",
    r"\bsudo\b",
    r"\bwget\b",
    r"\bssh\b",
    r"\bnc\s+-l\b",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
]

# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------

bash_tool = ToolDefinition(
    name="bash",
    description=(
        "Execute a shell command in the agent's container. "
        "Commands must pass an allowlist check. "
        "Hard-denied patterns are refused before execution. "
        "Commands not in the auto-approve list require user approval via the PWA."
    ),
    category="system_dangerous",
    parameters=[
        ToolParameter(
            name="command",
            type="string",
            description="Shell command to execute (no shell=True; parsed via shlex)",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="timeout_seconds",
            type="number",
            description=(
                f"Execution timeout in seconds (default {_DEFAULT_TIMEOUT}, "
                f"max {_MAX_TIMEOUT})"
            ),
            required=False,
            default=_DEFAULT_TIMEOUT,
            json_schema=None,
        ),
    ],
    risk_level="high",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED"],
    requires_approval=True,
    requires_sandbox=False,
    timeout_seconds=_DEFAULT_TIMEOUT,
    rate_limit_per_hour=200,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_deny_patterns() -> list[str]:
    """Load hard_deny_patterns from governance config, falling back to _FALLBACK_DENY.

    Returns:
        List of regex pattern strings that, if matched, hard-deny the command.
    """
    try:
        governance = load_governance_config()
        policy = governance.tools.get("bash")
        if policy is not None and policy.hard_deny_patterns:
            return policy.hard_deny_patterns
    except Exception as exc:  # noqa: BLE001 -- log and fall through
        log.warning("bash_governance_load_error", error=str(exc))
    return _FALLBACK_DENY


def _is_hard_denied(command: str, patterns: list[str]) -> str | None:
    """Return the first matching hard-deny pattern, or None if command is clean.

    Args:
        command: Raw command string to test.
        patterns: List of regex patterns (IGNORECASE applied).

    Returns:
        Matched pattern string if any pattern matches, else None.
    """
    for pattern in patterns:
        if re.search(pattern, command, re.IGNORECASE):
            return pattern
    return None


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def bash_executor(
    command: str,
    timeout_seconds: int = _DEFAULT_TIMEOUT,
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """Execute a shell command with hard-deny guards and output capping.

    Security guards fire in this order:
    1. Hard-deny regex check (never reaches subprocess on match).
    2. shlex parse (rejects malformed quoting).
    3. Empty-command guard.
    4. Subprocess execution with timeout.
    5. Output cap (50 KiB; overflow written to scratch).

    Args:
        command: Shell command string to execute. Parsed via ``shlex.split``
            so no ``shell=True`` injection surface exists.
        timeout_seconds: Max seconds to wait for the process. Clamped to
            [1, 120]; defaults to 30.
        ctx: Optional trace context for structured logging.

    Returns:
        Dict with keys:
        - ``success`` (bool): True when exit_code == 0.
        - ``exit_code`` (int): Process return code.
        - ``stdout`` (str): Captured stdout (possibly truncated).
        - ``stderr`` (str): Captured stderr (possibly truncated).
        - ``command`` (str): Original command string.
        - ``truncated_path`` (str | None): Path to overflow file if output
          exceeded 50 KiB, else None.

        On guard failures, returns a dict with ``success=False`` and an
        ``error`` key set to one of: ``hard_denied``, ``parse_error``,
        ``empty_command``, ``timeout``.
    """
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"

    # ------------------------------------------------------------------
    # 1. Hard-deny check (belt-and-suspenders before any subprocess)
    # ------------------------------------------------------------------
    deny_patterns = _load_deny_patterns()
    matched = _is_hard_denied(command, deny_patterns)
    if matched is not None:
        log.warning(
            "bash_hard_denied",
            trace_id=trace_id,
            command=command,
            pattern=matched,
        )
        return {
            "success": False,
            "error": "hard_denied",
            "pattern": matched,
            "command": command,
        }

    # ------------------------------------------------------------------
    # 2. Parse command via shlex (no shell=True)
    # ------------------------------------------------------------------
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return {
            "success": False,
            "error": "parse_error",
            "detail": str(exc),
            "command": command,
        }

    # ------------------------------------------------------------------
    # 3. Empty-command guard
    # ------------------------------------------------------------------
    if not argv:
        return {"success": False, "error": "empty_command", "command": command}

    # ------------------------------------------------------------------
    # 4. Clamp timeout
    # ------------------------------------------------------------------
    timeout_seconds = min(max(int(timeout_seconds), 1), _MAX_TIMEOUT)

    log.info(
        "bash_started",
        trace_id=trace_id,
        command=command,
        timeout_seconds=timeout_seconds,
    )

    # ------------------------------------------------------------------
    # 5. Execute
    # ------------------------------------------------------------------
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=float(timeout_seconds)
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning(
                "bash_timeout",
                trace_id=trace_id,
                command=command,
                timeout_seconds=timeout_seconds,
            )
            return {
                "success": False,
                "error": "timeout",
                "command": command,
                "timeout_seconds": timeout_seconds,
            }
    except OSError as exc:
        log.error("bash_os_error", trace_id=trace_id, command=command, error=str(exc))
        return {
            "success": False,
            "error": "os_error",
            "detail": str(exc),
            "command": command,
        }

    # ------------------------------------------------------------------
    # 6. Decode output
    # ------------------------------------------------------------------
    stdout_str = stdout_bytes.decode("utf-8", errors="replace")
    stderr_str = stderr_bytes.decode("utf-8", errors="replace")
    exit_code: int = proc.returncode if proc.returncode is not None else -1

    # ------------------------------------------------------------------
    # 7. Output cap (50 KiB combined)
    # ------------------------------------------------------------------
    combined = stdout_str + stderr_str
    truncated_path: str | None = None

    if len(combined.encode("utf-8")) > MAX_OUTPUT_BYTES:
        # Write overflow to scratch directory keyed by trace_id
        if ctx is not None:
            try:
                scratch = Path("/tmp/agent_scratch") / trace_id
                scratch.mkdir(parents=True, exist_ok=True)
                existing = list(scratch.glob("bash_output_*.txt"))
                n = len(existing)
                overflow_file = scratch / f"bash_output_{n}.txt"
                overflow_file.write_text(combined, encoding="utf-8")
                truncated_path = str(overflow_file)
                log.info(
                    "bash_output_overflow",
                    trace_id=trace_id,
                    overflow_file=truncated_path,
                    combined_len=len(combined),
                )
            except OSError as exc:
                log.warning(
                    "bash_overflow_write_error", trace_id=trace_id, error=str(exc)
                )

        # Truncate in-memory output to half the cap each
        half = MAX_OUTPUT_BYTES // 2
        stdout_str = stdout_str[:half]
        stderr_str = stderr_str[:half]

    log.info(
        "bash_completed",
        trace_id=trace_id,
        command=command,
        exit_code=exit_code,
        stdout_len=len(stdout_str),
        stderr_len=len(stderr_str),
        truncated=truncated_path is not None,
    )

    return {
        "success": exit_code == 0,
        "exit_code": exit_code,
        "stdout": stdout_str,
        "stderr": stderr_str,
        "command": command,
        "truncated_path": truncated_path,
    }
