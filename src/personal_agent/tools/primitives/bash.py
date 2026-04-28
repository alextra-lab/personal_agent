"""Sandboxed bash command executor primitive tool.

Provides a ``bash`` tool that executes shell commands in the agent's container
with an allowlist-based approval model and hard-deny patterns for truly
dangerous commands.

Security model
--------------
* Hard-deny regex patterns are checked *before* any subprocess is spawned.
  Even if governance config is misconfigured, these patterns prevent the most
  catastrophic commands from executing.
* Commands run via ``/bin/bash -o pipefail -c <command>`` — shell semantics are
  fully supported: pipes (``|``), logical operators (``&&``, ``||``), command
  separators (``;``), redirects, glob expansion, and env substitution all work.
  This is safe because no user-visible string is interpolated into the shell
  invocation itself; the command is passed as a single ``-c`` argument.
* Auto-approve logic (``_check_segment_allowlist``) splits the command on
  top-level operators and verifies the first word of every segment against the
  per-mode ``auto_approve_prefixes`` from ``tools.yaml``.  It is evaluated by
  the ``_check_permissions`` layer in ``tools/executor.py``, not by the executor.
* Timeout is clamped to [1, 120] seconds.
* Output is capped at 50 KiB (combined stdout + stderr); overflow is written to
  a scratch file and the path is returned.

FRE-261 Step 4 · FRE-283 (real shell contract).
"""

from __future__ import annotations

import asyncio
import re
import shlex
from pathlib import Path
from typing import Any

from personal_agent.config import load_governance_config
from personal_agent.config.governance_loader import GovernanceConfigError
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
        "Execute a shell command in the agent's container via /bin/bash. "
        "Pipes (|), &&, ||, ;, redirects, globs, and env expansion all work. "
        "Hard-deny patterns refuse catastrophic commands before the shell sees them. "
        "Commands whose every pipeline segment matches the auto-approve allowlist run "
        "without prompting; others require user approval via the PWA."
    ),
    category="system_dangerous",
    parameters=[
        ToolParameter(
            name="command",
            type="string",
            description="Shell command to run via /bin/bash (pipes and composition operators work)",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="timeout_seconds",
            type="number",
            description=(
                f"Execution timeout in seconds (default {_DEFAULT_TIMEOUT}, max {_MAX_TIMEOUT})"
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
    except GovernanceConfigError as exc:
        # Config directory missing, YAML parse error, or Pydantic validation failure.
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


def _split_command_segments(command: str) -> list[str]:
    """Split a shell command into pipeline / sequence segments.

    Splits on top-level ``|``, ``||``, ``&&``, and ``;`` while respecting
    single-quoted strings, double-quoted strings, and backslash escapes.
    Sub-shells (``$(…)`` or backticks) are treated as opaque and are NOT
    recursively split — their content is included in the surrounding segment.

    Args:
        command: Raw shell command string.

    Returns:
        Non-empty, stripped segment strings.  An empty command returns ``[]``.
    """
    segments: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(command)

    while i < n:
        c = command[i]

        if in_single:
            current.append(c)
            if c == "'":
                in_single = False
            i += 1
        elif in_double:
            if c == "\\" and i + 1 < n:
                # Backslash escapes are two characters inside double quotes.
                current.append(c)
                i += 1
                current.append(command[i])
                i += 1
            else:
                current.append(c)
                if c == '"':
                    in_double = False
                i += 1
        elif c == "\\" and i + 1 < n:
            current.append(c)
            i += 1
            current.append(command[i])
            i += 1
        elif c == "'":
            in_single = True
            current.append(c)
            i += 1
        elif c == '"':
            in_double = True
            current.append(c)
            i += 1
        elif c == ";":
            segments.append("".join(current).strip())
            current = []
            i += 1
        elif c == "|":
            if i + 1 < n and command[i + 1] == "|":
                segments.append("".join(current).strip())
                current = []
                i += 2
            else:
                segments.append("".join(current).strip())
                current = []
                i += 1
        elif c == "&":
            if i + 1 < n and command[i + 1] == "&":
                segments.append("".join(current).strip())
                current = []
                i += 2
            else:
                # Single ``&`` (background execution) — treat as part of segment.
                current.append(c)
                i += 1
        else:
            current.append(c)
            i += 1

    remaining = "".join(current).strip()
    if remaining:
        segments.append(remaining)

    return [s for s in segments if s]


def _check_segment_allowlist(command: str, allowlist: list[str]) -> str | None:
    """Check every pipeline segment's first word against the auto-approve allowlist.

    Multi-word allowlist entries (e.g. ``"psql -c"``, ``"docker ps"``) match
    when the segment begins with those exact words in order.

    Args:
        command: Raw shell command string.
        allowlist: Ordered list of allowed prefix strings for the current mode.

    Returns:
        The first non-matching segment string, or ``None`` if all segments pass
        (indicating the command may be auto-approved).
    """
    segments = _split_command_segments(command)
    for segment in segments:
        try:
            words = shlex.split(segment)
        except ValueError:
            # Unparseable segment — conservative: treat as not approved.
            return segment
        if not words:
            continue
        matched = any(
            words[: len(prefix_words)] == prefix_words
            for entry in allowlist
            if (prefix_words := entry.split())
        )
        if not matched:
            return segment
    return None


def _truncate_to_bytes(s: str, max_bytes: int) -> str:
    """Truncate a string to at most max_bytes when UTF-8 encoded.

    Args:
        s: Input string.
        max_bytes: Maximum byte length of the returned UTF-8 encoding.

    Returns:
        Possibly-shorter string that encodes to at most max_bytes bytes.
        If truncation splits a multi-byte character, the replacement character
        U+FFFD is used (via ``errors='replace'`` on decode).
    """
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def bash_executor(
    command: str,
    timeout_seconds: int = _DEFAULT_TIMEOUT,
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """Execute a shell command via ``/bin/bash`` with hard-deny guards and output capping.

    Security guards fire in this order:
    1. Hard-deny regex check on the raw command string (never reaches the shell on match).
    2. Empty-command guard.
    3. Subprocess via ``/bin/bash -o pipefail -c <command>`` with timeout.
    4. Output cap (50 KiB; overflow written to scratch).

    Pipes (``|``), logical operators (``&&``, ``||``), separators (``;``),
    redirects, glob expansion, and env substitution all work because the
    command is passed to a real ``/bin/bash`` process as a single ``-c``
    argument — nothing is interpolated into the invocation string itself.

    Auto-approve enforcement (``_check_segment_allowlist``) runs in the
    ``_check_permissions`` layer in ``tools/executor.py`` before this is called.

    Args:
        command: Shell command string to execute via ``/bin/bash -c``.
        timeout_seconds: Max seconds to wait for the process. Clamped to
            [1, 120]; defaults to 30.
        ctx: Optional trace context for structured logging.

    Returns:
        Dict with keys:
        - ``success`` (bool): True when exit_code == 0.
        - ``exit_code`` (int): Process return code (reflects ``pipefail`` semantics).
        - ``stdout`` (str): Captured stdout (possibly truncated).
        - ``stderr`` (str): Captured stderr (possibly truncated).
        - ``command`` (str): Original command string.
        - ``truncated_path`` (str | None): Path to overflow file if output
          exceeded 50 KiB, else None.

        On guard failures, returns a dict with ``success=False`` and an
        ``error`` key set to one of: ``hard_denied``, ``empty_command``, ``timeout``.
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
    # 2. Empty-command guard
    # ------------------------------------------------------------------
    if not command.strip():
        return {"success": False, "error": "empty_command", "command": command}

    # ------------------------------------------------------------------
    # 3. Clamp timeout
    # ------------------------------------------------------------------
    timeout_seconds = min(max(int(timeout_seconds), 1), _MAX_TIMEOUT)

    log.info(
        "bash_started",
        trace_id=trace_id,
        command=command,
        timeout_seconds=timeout_seconds,
    )

    # ------------------------------------------------------------------
    # 4. Execute via /bin/bash (real shell — pipes and composition work).
    #    -o pipefail: exit code reflects the worst-failing pipe segment.
    #    The command string is passed as the sole -c argument, so it is
    #    never interpolated into the invocation — no shell injection surface.
    # ------------------------------------------------------------------
    _shell = "/bin/bash"
    _flags = ("-o", "pipefail", "-c")
    try:
        proc = await asyncio.create_subprocess_exec(
            _shell,
            *_flags,
            command,
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
        # Write overflow to scratch directory keyed by trace_id (when ctx available).
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
                log.warning("bash_overflow_write_error", trace_id=trace_id, error=str(exc))
                truncated_path = "<truncated: scratch write failed>"
        else:
            # No trace context — caller must know data was discarded silently.
            truncated_path = "<truncated: no ctx>"

        # Byte-aware truncation: cap each stream at half the total limit.
        half = MAX_OUTPUT_BYTES // 2
        stdout_str = _truncate_to_bytes(stdout_str, half)
        stderr_str = _truncate_to_bytes(stderr_str, half)

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
