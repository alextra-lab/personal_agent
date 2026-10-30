"""Python sandbox executor primitive tool.

Runs arbitrary Python scripts inside a hardened Docker container.  The
container is non-root, read-only root filesystem, no network by default, and
limited to a scratch bind-mount at ``/sandbox``.

See :mod:`sandbox` for the low-level Docker invocation and security model.

FRE-261 Step 5. FRE-1575: one tool contract — a failure raises :class:`RunPythonError`; a
non-zero exit is a normal result, because the script ran.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Literal

from personal_agent.config import settings
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.contract import ToolError, error_codes
from personal_agent.tools.primitives.bash import _truncate_to_bytes
from personal_agent.tools.primitives.sandbox import run_in_sandbox
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_OUTPUT_BYTES = 51_200  # 50 KiB — same cap as bash tool
_DEFAULT_TIMEOUT = 60
_MAX_TIMEOUT = 300
_MIN_TIMEOUT = 5

# ---------------------------------------------------------------------------
# Failure codes (ADR-0156 D6.1)
# ---------------------------------------------------------------------------

RunPythonCode = Literal["sandbox_unavailable", "timeout", "out_of_memory"]


class RunPythonError(ToolError[RunPythonCode]):
    """A ``run_python`` failure: the script did not run to completion."""


_OUTPUT_TAIL_CHARS = 800

# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------

run_python_tool = ToolDefinition(
    name="run_python",
    description=(
        "Run a Python script in an isolated Docker sandbox and return stdout, stderr and "
        "exit_code. "
        "Use it for computation, data analysis and parsing, with pandas, numpy, requests, "
        "httpx, pyyaml and psutil installed. "
        "Each call is a fresh container: no variable or import carries over, so put the whole "
        "script in one call. "
        "The app source is not importable (from personal_agent fails), and /proc shows the "
        "sandbox, not the host: use bash free or ps for host metrics. "
        "Files written to /sandbox come back in scratch_files; the host filesystem is out of "
        "reach, so use write for a file that must persist. "
        "Network is off by default, and network=true always asks the user. "
        "Output is capped at 50 KiB. A non-zero exit_code is a normal result; a timeout "
        "(default 60 s, at most 300 s) or an out-of-memory kill is an error. "
        "Do not use it for shell commands (use bash) or to read a file (use read). "
        'Examples: run_python(script="print(2 ** 10)"), '
        "run_python(script=\"import pandas as pd\\nprint(pd.DataFrame({'x': [1, 2]})"
        '.describe())").'
    ),
    category="system_dangerous",
    parameters=[
        ToolParameter(
            name="script",
            type="string",
            description="Python script to execute inside the sandbox",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="timeout_seconds",
            type="number",
            description=(
                f"Timeout in seconds (default {_DEFAULT_TIMEOUT}, "
                f"min {_MIN_TIMEOUT}, max {_MAX_TIMEOUT})"
            ),
            required=False,
            default=_DEFAULT_TIMEOUT,
            json_schema=None,
        ),
        ToolParameter(
            name="network",
            type="boolean",
            description=(
                "Enable outbound network access (attaches to the configured sandbox network). "
                "Disabled by default. Always asks the user (ADR-0156 D8.6)."
            ),
            required=False,
            default=False,
            json_schema=None,
        ),
    ],
    risk_level="high",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=True,
    timeout_seconds=_DEFAULT_TIMEOUT,
    error_codes=error_codes(RunPythonError),
    max_result_tokens=16_000,
    truncation_next_action=(
        "Print less (a summary or the first rows), or write the full output to /sandbox and "
        "print its path."
    ),
)

# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def run_python_executor(
    script: str,
    timeout_seconds: int = _DEFAULT_TIMEOUT,
    network: bool = False,
    *,
    ctx: TraceContext,
) -> dict[str, Any]:
    """Execute *script* in the Python Docker sandbox.

    Security guards fire in this order:
    1. ``docker`` binary availability check.
    2. Timeout clamped to [5, 300].
    3. Per-trace scratch directory created under ``settings.sandbox_scratch_root``.
    4. Sandbox invoked via :func:`run_in_sandbox`.
    5. Output capped at 50 KiB (combined); overflow truncated with a note.

    Args:
        script: Python source code to run inside the container.
        timeout_seconds: Execution timeout.  Clamped to [5, 300]; defaults to 60.
        network: When True the container is attached to the ``cloud-sim`` Docker
            network (outbound access).  When False ``--network=none`` is used.
        ctx: Trace context used for structured logging and scratch-dir
            scoping.

    Returns:
        Dict with keys:
        - ``exit_code`` (int): Container exit code. A non-zero exit is a normal result.
        - ``stdout`` (str): Captured stdout (possibly truncated to 25 KiB).
        - ``stderr`` (str): Captured stderr (possibly truncated to 25 KiB).
        - ``scratch_files`` (list[str]): Host paths of files in scratch dir.
        - ``truncated`` (bool): True when output was truncated.

    Raises:
        RunPythonError: ``sandbox_unavailable`` (no docker), ``timeout``, or
            ``out_of_memory`` (exit 137). The message carries the output tail.
    """
    trace_id = ctx.trace_id

    # ------------------------------------------------------------------
    # 1. Docker binary check
    # ------------------------------------------------------------------
    if shutil.which("docker") is None:
        log.warning("run_python_sandbox_unavailable", trace_id=trace_id)
        raise RunPythonError(
            "sandbox_unavailable",
            "docker binary not found — run_python requires Docker",
            retryable=False,
            next_action="run_python cannot run here. Do the work another way or tell the user.",
        )

    # ------------------------------------------------------------------
    # 2. Clamp timeout
    # ------------------------------------------------------------------
    timeout_seconds = min(max(int(timeout_seconds), _MIN_TIMEOUT), _MAX_TIMEOUT)

    # ------------------------------------------------------------------
    # 3. Per-trace scratch directory
    # ------------------------------------------------------------------
    scratch_dir = Path(settings.sandbox_scratch_root) / trace_id
    # mkdir is handled inside run_in_sandbox; we just pass the path.

    log.info(
        "run_python_started",
        trace_id=trace_id,
        timeout_seconds=timeout_seconds,
        network=network,
        scratch_dir=str(scratch_dir),
        image=settings.sandbox_image,
    )

    # ------------------------------------------------------------------
    # 4. Execute in sandbox
    # ------------------------------------------------------------------
    result = await run_in_sandbox(
        image=settings.sandbox_image,
        script=script,
        timeout_seconds=timeout_seconds,
        network=network,
        scratch_host_path=scratch_dir,
        trace_id=trace_id,
        tool="run_python",
    )

    if result.timed_out:
        # Durable, tool-named, turn-named, limit-named record (FRE-1462 AC-4)
        # distinct from the generic per-call info log below.
        log.warning(
            "run_python_terminated",
            trace_id=trace_id,
            timeout_seconds=timeout_seconds,
            tool="run_python",
        )

    # ------------------------------------------------------------------
    # 5. Output cap (50 KiB combined; 25 KiB per stream)
    # ------------------------------------------------------------------
    half = MAX_OUTPUT_BYTES // 2
    stdout_str = result.stdout
    stderr_str = result.stderr
    truncated = False

    combined_len = len((stdout_str + stderr_str).encode("utf-8"))
    if combined_len > MAX_OUTPUT_BYTES:
        stdout_str = _truncate_to_bytes(stdout_str, half)
        stderr_str = _truncate_to_bytes(stderr_str, half)
        truncated = True
        log.info(
            "run_python_output_truncated",
            trace_id=trace_id,
            original_bytes=combined_len,
            cap_bytes=MAX_OUTPUT_BYTES,
        )

    log.info(
        "run_python_finished",
        trace_id=trace_id,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        oom=result.oom,
        truncated=truncated,
        scratch_files_count=len(result.scratch_files),
    )

    output_tail = (stdout_str + stderr_str)[-_OUTPUT_TAIL_CHARS:]
    if result.timed_out:
        raise RunPythonError(
            "timeout",
            f"The script did not finish within {timeout_seconds} seconds and was stopped. "
            f"Output tail: {output_tail}",
            retryable=True,
            next_action=(
                f"Make the script faster or smaller, or raise timeout_seconds (at most "
                f"{_MAX_TIMEOUT})."
            ),
        )
    if result.oom:
        raise RunPythonError(
            "out_of_memory",
            f"The sandbox killed the script for using too much memory. Output tail: {output_tail}",
            retryable=False,
            next_action="Process the data in smaller pieces, then call run_python again.",
        )

    return {
        "exit_code": result.exit_code,
        "stdout": stdout_str,
        "stderr": stderr_str,
        "scratch_files": result.scratch_files,
        "truncated": truncated,
    }
