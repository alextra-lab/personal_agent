"""Native system diagnostics tool (FRE-188 / ADR-0028).

Single subprocess-based tool that dispatches to an allow-listed set of
read-only OS diagnostic commands (ps, lsof, iostat, df, …).

Security model
--------------
* Allow-list controls which binaries can be invoked — no shell=True.
* Args are parsed via shlex and passed directly to asyncio.create_subprocess_exec
  so there is no shell injection surface.
* Binary paths are resolved via shutil.which at import time (not at call time),
  preventing runtime PATH manipulation.
* Timeout defaults to 15 s; callers can override up to 60 s.
* Governance mode restrictions apply via the standard ToolExecutionLayer.

Platform support
----------------
Detected at import time.  Darwin: macOS-specific commands (vm_stat, sw_vers,
diskutil, top -l).  Linux/Debian: Linux equivalents (vmstat, free, ss, ip,
top -b).  Commands unavailable on the host are silently excluded.
"""

from __future__ import annotations

import asyncio
import platform
import shlex
import shutil
from typing import Any

from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

_DEFAULT_TIMEOUT = 15
_MAX_TIMEOUT = 60
_MAX_OUTPUT_CHARS = 32_000

# ---------------------------------------------------------------------------
# Platform-specific allow-list
# Paths resolved via shutil.which at import time — safe because resolution
# happens before any user input arrives, so PATH cannot be manipulated.
# Commands unavailable on the host are silently excluded from the allow-list.
# ---------------------------------------------------------------------------

_IS_DARWIN = platform.system() == "Darwin"

_DARWIN_CANDIDATES = [
    "ps", "pgrep", "top", "lsof",
    "find", "df", "du",
    "iostat", "vm_stat",
    "ifconfig", "netstat",
    "uptime", "sysctl", "who", "last",
    "sw_vers", "diskutil",
]

_LINUX_CANDIDATES = [
    "ps", "pgrep", "top", "lsof",
    "find", "df", "du",
    "iostat", "vmstat", "free",
    "ip", "ifconfig", "ss", "netstat",
    "uptime", "sysctl", "who", "last",
    "uname",
]

_candidates = _DARWIN_CANDIDATES if _IS_DARWIN else _LINUX_CANDIDATES

# Build allow-list: name → resolved absolute path (excludes unavailable binaries).
_ALLOW_LIST: dict[str, str] = {
    cmd: path
    for cmd in _candidates
    if (path := shutil.which(cmd)) is not None
}

_ALLOWED_NAMES = ", ".join(sorted(_ALLOW_LIST))

def _build_description() -> str:
    base = (
        "Run a read-only system diagnostic command on the host machine. "
        "Returns stdout, stderr, and exit code. "
        f"Allowed commands:\n  {_ALLOWED_NAMES}\n\n"
        "Common usage patterns:\n"
        "- Process list (all): ps aux\n"
        "- Process search by name: pgrep -lf python\n"
        "- Port listeners: lsof -i :9000\n"
        "- Open files by process: lsof -p <pid>\n"
        "- Disk usage: df -h\n"
        "- Directory size: du -sh /path/to/dir\n"
        "- File search: find /var/log -name '*.log' -mtime -1\n"
    )
    if _IS_DARWIN:
        return base + (
            "- I/O stats: iostat -d 1 3\n"
            "- Memory stats: vm_stat\n"
            "- Network interfaces: ifconfig\n"
            "- Network connections: netstat -an\n"
            "- Kernel params: sysctl kern.maxfiles\n"
            "- macOS version: sw_vers\n"
            "- Disk list: diskutil list\n"
            "Note: 'top' requires non-interactive flags on macOS: top -l 1 -n 20"
        )
    else:
        return base + (
            "- I/O stats: iostat -xz 1 3\n"
            "- Virtual memory / swap: vmstat 1 3\n"
            "- Memory overview: free -h\n"
            "- Network sockets/listeners: ss -tlnp\n"
            "- Network interfaces: ip addr\n"
            "- Kernel params: sysctl vm.swappiness\n"
            "- System info: uname -a\n"
            "Note: 'top' requires batch mode on Linux: top -b -n 1 -o %CPU"
        )


run_sysdiag_tool = ToolDefinition(
    name="run_sysdiag",
    description=_build_description(),
    category="read_only",
    parameters=[
        ToolParameter(
            name="command",
            type="string",
            description=(f"Command to run. Must be one of: {_ALLOWED_NAMES}."),
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="args",
            type="string",
            description=(
                "Arguments to pass to the command as a single string "
                "(shell-style quoting supported). "
                "Example: '-i :9000' for lsof, 'aux' for ps."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="timeout",
            type="number",
            description=f"Max seconds to wait (1–{_MAX_TIMEOUT}, default {_DEFAULT_TIMEOUT}).",
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=_MAX_TIMEOUT + 5,
    rate_limit_per_hour=300,
)


async def run_sysdiag_executor(
    command: str = "",
    args: str | None = None,
    timeout: int | None = None,
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """Execute an allow-listed system diagnostic command.

    Args:
        command: Command name (must be in the allow-list).
        args: Shell-style argument string (e.g. '-i :9000', 'aux').
        timeout: Seconds before the process is killed (default 15, max 60).
        ctx: Optional trace context for structured logging.

    Returns:
        Dict with ``command_used``, ``stdout``, ``stderr``, ``exit_code``,
        and ``truncated`` (bool, True if output was capped at 32,000 chars).

    Raises:
        ToolExecutionError: When the command is not in the allow-list,
            the process times out, or an OS-level error occurs.
    """
    command = (command or "").strip().lower()
    if command not in _ALLOW_LIST:
        raise ToolExecutionError(
            f"Command '{command}' is not available on this platform "
            f"({'Darwin' if _IS_DARWIN else 'Linux'}). "
            f"Allowed: {_ALLOWED_NAMES}."
        )

    binary = _ALLOW_LIST[command]
    timeout_s = max(1, min(int(timeout or _DEFAULT_TIMEOUT), _MAX_TIMEOUT))
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"

    # Parse args safely — no shell=True, so no injection surface
    parsed_args: list[str] = []
    if args and args.strip():
        try:
            parsed_args = shlex.split(args.strip())
        except ValueError as exc:
            raise ToolExecutionError(f"Cannot parse args '{args}': {exc}") from exc

    full_argv = [binary, *parsed_args]
    command_used = " ".join(shlex.quote(a) for a in full_argv)

    log.info(
        "run_sysdiag_started",
        trace_id=trace_id,
        command=command,
        args=args,
        timeout=timeout_s,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *full_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            raw_stdout, raw_stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise ToolExecutionError(
                f"'{command}' timed out after {timeout_s}s. "
                "Use a more specific query or increase timeout."
            ) from None

    except ToolExecutionError:
        raise
    except OSError as exc:
        log.error("run_sysdiag_os_error", trace_id=trace_id, command=command, error=str(exc))
        raise ToolExecutionError(f"OS error running '{command}': {exc}") from exc
    except Exception as exc:
        log.error("run_sysdiag_failed", trace_id=trace_id, command=command, error=str(exc))
        raise ToolExecutionError(str(exc)) from exc

    stdout = raw_stdout.decode("utf-8", errors="replace")
    stderr = raw_stderr.decode("utf-8", errors="replace")
    exit_code: int = proc.returncode if proc.returncode is not None else -1

    combined_len = len(stdout) + len(stderr)
    truncated = combined_len > _MAX_OUTPUT_CHARS
    if truncated:
        # Trim stdout first, then stderr
        if len(stdout) > _MAX_OUTPUT_CHARS:
            stdout = stdout[:_MAX_OUTPUT_CHARS] + "\n[... truncated]"
            stderr = ""
        else:
            remaining = _MAX_OUTPUT_CHARS - len(stdout)
            stderr = stderr[:remaining] + "\n[... truncated]"

    log.info(
        "run_sysdiag_completed",
        trace_id=trace_id,
        command=command,
        exit_code=exit_code,
        output_chars=combined_len,
        truncated=truncated,
    )

    return {
        "command_used": command_used,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "truncated": truncated,
    }
