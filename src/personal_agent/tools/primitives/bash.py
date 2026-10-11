"""Sandboxed bash command executor primitive tool.

Provides a ``bash`` tool that executes shell commands in the agent's container.

Security model
--------------
* Whether a command runs, asks or is refused is decided before this executor is
  called, by the permission rules (``config/governance/permissions.yaml``, ADR-0156
  D8) in ``ToolExecutionLayer._check_permissions``. The command is parsed with a
  shell parser, and each subcommand must match an allow rule as a plain command; any
  other command asks the user. The former ``hard_deny_patterns`` are deny rules there
  (FRE-1574).
* Commands run via ``/bin/bash -o pipefail -c <command>`` — shell semantics are
  fully supported: pipes (``|``), logical operators (``&&``, ``||``), command
  separators (``;``), redirects, glob expansion, and env substitution all work.
  This is safe because no user-visible string is interpolated into the shell
  invocation itself; the command is passed as a single ``-c`` argument.
* Timeout is clamped to [1, 120] seconds.
* Output is capped at 50 KiB (combined stdout + stderr); overflow is written to
  a scratch file and the path is returned.
* On an eval deployment (``deployment_profile == "eval"``) the child runs as the
  unprivileged ``nobody`` account with an allowlisted environment (FRE-1505). The
  eval-treatment gateway runs bash with no human approval, so the child must not
  read a credential: a non-root process cannot open ``/proc/<pid>/environ`` of any
  root process (the gateway, PID 1, health checks, sibling children), and the
  kernel clears capabilities on the uid change. If the gateway is not root it
  cannot drop privileges, so eval bash refuses and spawns nothing. The identity
  change runs through ``setpriv`` because the served event loop (uvloop) rejects the
  ``user``/``group``/``extra_groups`` spawn kwargs (FRE-1518).

FRE-261 Step 4 · FRE-283 (real shell contract) · FRE-1505 (eval credential isolation) ·
FRE-1518 (isolation on the served event loop) · FRE-1574 (one permission model) ·
FRE-1575 (one tool contract: a guard failure raises :class:`BashError`; a non-zero exit is a
normal result, because the tool ran the command).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Literal

from personal_agent.config import settings
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.contract import ToolError, error_codes
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_OUTPUT_BYTES = 51_200  # 50 KiB
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120

# Exit code for a process terminated by SIGPIPE (signal 13): 128 + 13 = 141.
# Under ``-o pipefail`` this surfaces when a downstream pipeline consumer
# (``head``, ``grep -q``, …) closes the pipe early and the upstream producer
# is killed.  That is benign early-exit behavior, not a command failure. FRE-470.
_SIGPIPE_EXIT_CODE = 141

# FRE-1505: identity and environment of the bash child on an eval deployment.
# 65534 is ``nobody``/``nogroup`` in the Debian-based gateway image.
EVAL_CHILD_UID = 65534
EVAL_CHILD_GID = 65534
# An allowlist, not a denylist: AGENT_DATABASE_URL carries a password under a name no
# suffix pattern matches. HOME and TMPDIR are set to /tmp, which ``nobody`` can write.
EVAL_CHILD_ENV_NAMES: frozenset[str] = frozenset(
    {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "VIRTUAL_ENV"}
)
_EVAL_CHILD_HOME = "/tmp"
# setpriv (util-linux) changes identity and then execs bash, so the spawned PID is bash.
# All capability sets are cleared, and no_new_privs stops a setuid binary regaining root.
EVAL_CHILD_SETPRIV_ARGV: tuple[str, ...] = (
    "/usr/bin/setpriv",
    f"--reuid={EVAL_CHILD_UID}",
    f"--regid={EVAL_CHILD_GID}",
    "--clear-groups",
    "--inh-caps=-all",
    "--ambient-caps=-all",
    "--bounding-set=-all",
    "--no-new-privs",
    "--",
)

# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------

BashCode = Literal["empty_command", "timeout", "os_error", "credential_isolation_unavailable"]


class BashError(ToolError[BashCode]):
    """A ``bash`` failure: the command did not run to completion."""


bash_tool = ToolDefinition(
    name="bash",
    description=(
        "Run a shell command in the agent's container with /bin/bash -o pipefail -c; pipes, "
        "&&, ||, ;, redirects and globs work. "
        "Commands start in /app; use absolute paths (for example /app/src, /app/config, "
        "/app/telemetry). "
        "A command runs without asking only when every subcommand is a plain command that an "
        "allow rule covers, such as ls, cat, grep, find, head, tail, wc, sort, jq, ps, df or "
        "free. "
        "curl runs without asking only for a read of internal telemetry (Elasticsearch and "
        "Tempo reads, and the health endpoints) at one plain http://host:port/path URL; "
        "every other curl asks. "
        "A plain command has literal words only: no $VAR or $(...), no unquoted glob such as "
        "*.py (quote it: '*.py'), no ~, no VAR=x in front, no here-doc, and no redirection "
        "except > /dev/null, 2> /dev/null and 2>&1. "
        "Any other command asks the user on an approval card; python3, awk, sed, env, psql "
        "and redis-cli always ask, and rm -rf, sudo, wget, ssh and dd are always refused. "
        "A refusal names the subcommand and the rule: do not retry it, use a covered command. "
        "To count files use find <dir> -name '*.yaml' -type f | wc -l, not ls -R. "
        "Output is capped at 50 KiB (the full text goes to truncated_path). A non-zero "
        "exit_code is a normal result; exit 141 in a pipeline (head closed the pipe) counts "
        "as success. "
        "Do not use it to read a file that read can open, or to fetch a web page (use "
        "fetch_url). "
        "Examples: bash(command=\"grep -rn 'def bash_executor' /app/src/\"), "
        "bash(command=\"find /app/config -name '*.yaml' -type f | wc -l\"), "
        'bash(command="curl -s http://elasticsearch:9200/_cluster/health | jq .status").'
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
    error_codes=error_codes(BashError),
    max_result_tokens=16_000,
    truncation_next_action=(
        "Narrow the command (grep, head, a smaller path) and run it again. If truncated_path "
        "is a path, read the full output there with offset and limit."
    ),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_top_level_pipe(command: str) -> bool:
    """Return True if the command contains a top-level pipe (``|``, not ``||``).

    Quote- and escape-aware: a ``|`` inside single quotes, double quotes, or
    preceded by a backslash is not a pipeline operator.  A ``||`` (logical OR)
    is not a pipe.  Mirrors the quote handling of the old segment splitter.

    Used to gate SIGPIPE (exit 141) leniency: exit 141 is only treated as benign
    when the command actually pipes, so a standalone ``exit 141`` or a
    non-pipeline command killed by SIGPIPE still reports failure (FRE-470).

    Args:
        command: Raw shell command string.

    Returns:
        True if a top-level single ``|`` appears outside quotes, else False.
    """
    in_single = False
    in_double = False
    i = 0
    n = len(command)

    while i < n:
        c = command[i]
        if in_single:
            if c == "'":
                in_single = False
            i += 1
        elif in_double:
            if c == "\\" and i + 1 < n:
                i += 2
            else:
                if c == '"':
                    in_double = False
                i += 1
        elif c == "\\" and i + 1 < n:
            i += 2
        elif c == "'":
            in_single = True
            i += 1
        elif c == '"':
            in_double = True
            i += 1
        elif c == "|":
            if i + 1 < n and command[i + 1] == "|":
                # Logical OR, not a pipe.
                i += 2
            else:
                return True
        else:
            i += 1

    return False


def eval_child_env() -> dict[str, str]:
    """Build the bash child's environment for an eval deployment.

    Returns:
        Only the allowlisted variables from this process's environment, plus
        ``HOME`` and ``TMPDIR`` set to a directory the unprivileged child can write.
    """
    env = {name: value for name, value in os.environ.items() if name in EVAL_CHILD_ENV_NAMES}
    env["HOME"] = _EVAL_CHILD_HOME
    env["TMPDIR"] = _EVAL_CHILD_HOME
    return env


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
    *,
    ctx: TraceContext,
) -> dict[str, Any]:
    """Execute a shell command via ``/bin/bash`` with output capping.

    Guards fire in this order:
    1. Empty-command guard.
    2. Subprocess via ``/bin/bash -o pipefail -c <command>`` with timeout.
    3. Output cap (50 KiB; overflow written to scratch).

    Pipes (``|``), logical operators (``&&``, ``||``), separators (``;``),
    redirects, glob expansion, and env substitution all work because the
    command is passed to a real ``/bin/bash`` process as a single ``-c``
    argument — nothing is interpolated into the invocation string itself.

    The permission rules run in ``ToolExecutionLayer._check_permissions`` before this is
    called (ADR-0156 D8).

    Args:
        command: Shell command string to execute via ``/bin/bash -c``.
        timeout_seconds: Max seconds to wait for the process. Clamped to
            [1, 120]; defaults to 30.
        ctx: Trace context for structured logging.

    Returns:
        Dict with keys:
        - ``exit_code`` (int): Process return code (reflects ``pipefail`` semantics).
          A non-zero exit is a normal result: the tool ran the command.
        - ``stdout`` (str): Captured stdout (possibly truncated).
        - ``stderr`` (str): Captured stderr (possibly truncated).
        - ``command`` (str): Original command string.
        - ``truncated_path`` (str | None): Path to overflow file if output
          exceeded 50 KiB, else None.
        - ``note`` (str | None): Explanatory note when a benign SIGPIPE exit (141 in a
          pipeline, FRE-470) was reinterpreted as success, else None.

    Raises:
        BashError: ``empty_command``, ``timeout``, ``os_error``, or
            ``credential_isolation_unavailable`` (eval deployment, gateway not root).
    """
    trace_id = ctx.trace_id

    # ------------------------------------------------------------------
    # 2. Empty-command guard
    # ------------------------------------------------------------------
    if not command.strip():
        raise BashError(
            "empty_command",
            "The command is empty.",
            retryable=True,
            next_action="Call bash again with a command.",
        )

    # ------------------------------------------------------------------
    # 3. Clamp timeout
    # ------------------------------------------------------------------
    timeout_seconds = min(max(int(timeout_seconds), 1), _MAX_TIMEOUT)

    # ------------------------------------------------------------------
    # 3a. Eval credential isolation (FRE-1505) — fail closed.
    # ------------------------------------------------------------------
    eval_isolation = settings.deployment_profile == "eval"
    if eval_isolation and os.geteuid() != 0:
        log.error(
            "bash_credential_isolation_unavailable",
            trace_id=trace_id,
            euid=os.geteuid(),
        )
        raise BashError(
            "credential_isolation_unavailable",
            "bash on an eval deployment must drop to an unprivileged account, "
            "which needs a root gateway; no command was run.",
            retryable=False,
            next_action="bash cannot run on this deployment. Use another tool.",
        )
    child_env = eval_child_env() if eval_isolation else None
    # setpriv, not the user/group/extra_groups kwargs: uvloop (the served loop)
    # rejects those kwargs even when they are None (FRE-1518).
    child_prefix = EVAL_CHILD_SETPRIV_ARGV if eval_isolation else ()

    log.info(
        "bash_started",
        trace_id=trace_id,
        command=command,
        timeout_seconds=timeout_seconds,
        credential_isolation=eval_isolation,
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
            *child_prefix,
            _shell,
            *_flags,
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
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
            raise BashError(
                "timeout",
                f"The command did not finish within {timeout_seconds} seconds and was killed: "
                f"{command}",
                retryable=True,
                next_action=(
                    f"Narrow the command, or raise timeout_seconds (at most {_MAX_TIMEOUT})."
                ),
            ) from None
    except OSError as exc:
        log.error("bash_os_error", trace_id=trace_id, command=command, error=str(exc))
        raise BashError(
            "os_error",
            f"The shell could not start: {exc}",
            retryable=True,
            next_action="Call bash once more; if it fails again, tell the user.",
        ) from exc

    # ------------------------------------------------------------------
    # 6. Decode output
    # ------------------------------------------------------------------
    stdout_str = stdout_bytes.decode("utf-8", errors="replace")
    stderr_str = stderr_bytes.decode("utf-8", errors="replace")
    exit_code: int = proc.returncode if proc.returncode is not None else -1

    # ------------------------------------------------------------------
    # 6a. Benign-SIGPIPE reinterpretation (FRE-470).
    #     Exit 141 (128 + SIGPIPE 13) inside a pipeline means a downstream
    #     consumer closed the pipe early and killed the upstream producer —
    #     normal early-exit behavior, not a failure.  We gate on the command
    #     actually being a pipeline so a standalone `exit 141`, or a non-pipe
    #     command killed by SIGPIPE, still reports failure (no masking).
    # ------------------------------------------------------------------
    note: str | None = None
    if exit_code == _SIGPIPE_EXIT_CODE and _has_top_level_pipe(command):
        note = (
            "exit 141 (SIGPIPE): a downstream pipeline consumer closed the pipe "
            "early, killing the upstream producer; treated as success."
        )
    # ------------------------------------------------------------------
    # 7. Output cap (50 KiB combined)
    # ------------------------------------------------------------------
    combined = stdout_str + stderr_str
    truncated_path: str | None = None

    if len(combined.encode("utf-8")) > MAX_OUTPUT_BYTES:
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
        sigpipe_success=note is not None,
    )

    return {
        "exit_code": exit_code,
        "stdout": stdout_str,
        "stderr": stderr_str,
        "command": command,
        "truncated_path": truncated_path,
        "note": note,
    }
