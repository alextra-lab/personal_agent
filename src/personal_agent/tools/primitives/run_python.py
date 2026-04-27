"""Python sandbox executor primitive tool.

Runs arbitrary Python scripts inside a hardened Docker container.  The
container is non-root, read-only root filesystem, no network by default, and
limited to a scratch bind-mount at ``/sandbox``.

See :mod:`sandbox` for the low-level Docker invocation and security model.

FRE-261 Step 5.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from personal_agent.config import settings
from personal_agent.telemetry import TraceContext, get_logger
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
# ToolDefinition
# ---------------------------------------------------------------------------

run_python_tool = ToolDefinition(
    name="run_python",
    description=(
        "Execute a Python script in an isolated Docker sandbox "
        "(non-root, no network by default, read-only root filesystem, "
        "``/sandbox`` scratch directory). "
        "Returns stdout, stderr, and exit_code. "
        "Use for computation, data transformation, or inspection tasks. "
        "Pre-installed: requests, httpx, pandas, numpy, pyyaml."
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
                "Enable outbound network access (attaches to cloud-sim network). "
                "Disabled by default. Requires approval in ALERT/DEGRADED modes."
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
    rate_limit_per_hour=60,
)

# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def run_python_executor(
    script: str,
    timeout_seconds: int = _DEFAULT_TIMEOUT,
    network: bool = False,
    ctx: TraceContext | None = None,
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
        ctx: Optional trace context used for structured logging and scratch-dir
            scoping.

    Returns:
        Dict with keys:
        - ``success`` (bool): True when ``exit_code == 0``.
        - ``exit_code`` (int): Container exit code.
        - ``stdout`` (str): Captured stdout (possibly truncated to 25 KiB).
        - ``stderr`` (str): Captured stderr (possibly truncated to 25 KiB).
        - ``timed_out`` (bool): True when container hit the timeout.
        - ``oom`` (bool): True when container was OOM-killed (exit 137).
        - ``scratch_files`` (list[str]): Host paths of files in scratch dir.
        - ``truncated`` (bool): True when output was truncated.

        On pre-flight failures, returns ``{"success": False, "error": "<key>",
        "detail": "<message>"}``.
    """
    trace_id = getattr(ctx, "trace_id", "no_trace") if ctx else "no_trace"

    # ------------------------------------------------------------------
    # 1. Docker binary check
    # ------------------------------------------------------------------
    if shutil.which("docker") is None:
        log.warning("run_python_sandbox_unavailable", trace_id=trace_id)
        return {
            "success": False,
            "error": "sandbox_unavailable",
            "detail": "docker binary not found — run_python requires Docker",
        }

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

    return {
        "success": result.exit_code == 0,
        "exit_code": result.exit_code,
        "stdout": stdout_str,
        "stderr": stderr_str,
        "timed_out": result.timed_out,
        "oom": result.oom,
        "scratch_files": result.scratch_files,
        "truncated": truncated,
    }
