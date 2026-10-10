"""Primitive write tool — supersedes ``write_file``.

Writes content to a file (overwrite or append mode) with explicit path-governance
checks (allowed_paths / forbidden_paths / unattended_paths from
``config/governance/tools.yaml``).

FRE-261 Step 3. FRE-1575: one tool contract — a failure raises :class:`WriteError`.
"""

from pathlib import Path
from typing import Any, Literal

import structlog

from personal_agent.config import load_governance_config
from personal_agent.telemetry import TraceContext
from personal_agent.tools.contract import ToolError, error_codes
from personal_agent.tools.primitives._governance import (
    PathCode,
    _check_durability,
    _check_path_governance,
    _expand_path,
    _matches_any,
)
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Failure codes (ADR-0156 D6.1)
# ---------------------------------------------------------------------------

WriteCode = Literal[PathCode, "invalid_mode", "permission_denied", "io_error"]


class WriteError(ToolError[WriteCode]):
    """A ``write`` failure."""


# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------

write_tool = ToolDefinition(
    name="write",
    description=(
        "Write text to a file and create its parent directories. "
        "Mode 'overwrite' (the default) replaces the whole file; 'append' adds to its end. "
        "Durable paths survive a restart: /app/agent_workspace/** and /app/telemetry/**. "
        "Any other path under /app/ is refused, because a rebuild destroys it; use /tmp/ for "
        "scratch work that need not survive. "
        "Do not use it to read a file (use read) or to run code (use run_python). "
        "A path outside the allowed paths is refused. "
        'Examples: write(path="/app/agent_workspace/notes/plan.md", content="# Plan\n...", '
        'mode="overwrite"), write(path="/tmp/run-log.md", content="\n## Step 2\n...", '
        'mode="append").'
    ),
    category="system_write",
    parameters=[
        ToolParameter(
            name="path",
            type="string",
            description="Absolute or home-relative path to write",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="content",
            type="string",
            description="Text content to write",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="mode",
            type="string",
            description="'overwrite' (default) or 'append'",
            required=False,
            default="overwrite",
            json_schema=None,
        ),
    ],
    risk_level="high",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "RECOVERY"],
    requires_approval=False,  # approval is via governance YAML + executor.py
    requires_sandbox=False,
    timeout_seconds=10,
    rate_limit_per_hour=None,
    error_codes=error_codes(WriteError),
    max_result_tokens=1_000,
)


# ---------------------------------------------------------------------------
# Write-specific governance helper
# ---------------------------------------------------------------------------


def _is_unattended_path(
    resolved: Path, tool_name: str = "write", *, trace_id: str | None = None
) -> bool:
    """Check whether *resolved* is under any ``unattended_paths`` for *tool_name*.

    Scratch/unattended paths (e.g. ``/tmp/**``) allow ``write`` to proceed
    without an additional approval flag in NORMAL mode.

    Args:
        resolved: Fully resolved absolute path.
        tool_name: Key to look up in ``governance_config.tools``.
        trace_id: Originating request trace_id, threaded onto the
            governance-load failure log for §I3 identity threading.

    Returns:
        True when the path is within an unattended scratch area.
    """
    try:
        governance = load_governance_config()
    except Exception as exc:  # noqa: BLE001
        log.warning("write_unattended_check_error", error=str(exc), trace_id=trace_id)
        return False

    policy = governance.tools.get(tool_name)
    if policy is None or not policy.unattended_paths:
        return False

    path_str = str(resolved)
    return _matches_any(path_str, policy.unattended_paths)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def write_executor(
    path: str,
    content: str,
    mode: str = "overwrite",
    *,
    ctx: TraceContext,
) -> dict[str, Any]:
    """Execute the ``write`` primitive tool.

    Writes *content* to the file at *path*, after applying path-governance
    checks from ``config/governance/tools.yaml``.  Parent directories are
    created automatically.

    Args:
        path: Absolute or home-relative path to write.
        content: Text content to write.
        mode: ``'overwrite'`` (default) replaces the file; ``'append'`` adds
            to the end.
        ctx: Trace context for structured logging correlation.

    Returns:
        ``{"path": str, "bytes_written": int, "mode": str}``. When the path is outside all
        ``unattended_paths`` an extra advisory key ``requires_approval_in_normal_mode: True``
        is included.

    Raises:
        PathRefusalError: The path rules or the durability check refuse the path.
        WriteError: ``invalid_mode``, ``permission_denied`` or ``io_error``.
    """
    trace_id = ctx.trace_id

    # 1. Validate mode
    if mode not in ("overwrite", "append"):
        raise WriteError(
            "invalid_mode",
            f"mode must be 'overwrite' or 'append', got {mode!r}",
            retryable=True,
            next_action="Call write again with mode='overwrite' or mode='append'.",
        )

    # 2. Resolve path
    resolved = Path(_expand_path(path)).expanduser().resolve()
    log.debug(
        "write_executor_called", path=path, resolved=str(resolved), mode=mode, trace_id=trace_id
    )

    # 3. Path governance
    try:
        _check_path_governance(resolved, tool_name="write", trace_id=trace_id)
    except ToolError as exc:
        log.warning(
            "write_path_rejected",
            reason=exc.code,
            path=str(resolved),
            trace_id=trace_id,
        )
        raise

    # 3b. Durability (FRE-1352) — refuse a write into the ephemeral container layer
    # under /app; only bind-mounted subdirectories survive a restart.
    _check_durability(resolved, tool_name="write", trace_id=trace_id)

    # 4. Scratch-dir / unattended detection
    unattended = _is_unattended_path(resolved, tool_name="write", trace_id=trace_id)

    # 5. Create parent directories, then write
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if mode == "overwrite":
            resolved.write_text(content, encoding="utf-8")
        else:  # append
            with open(resolved, "a", encoding="utf-8") as fh:
                fh.write(content)
    except PermissionError as exc:
        log.warning(
            "write_permission_denied", path=str(resolved), error=str(exc), trace_id=trace_id
        )
        raise WriteError(
            "permission_denied",
            f"Cannot write {str(resolved)!r}: {exc}",
            retryable=False,
            next_action="Do not retry this path. Write under /app/agent_workspace/ or /tmp/.",
        ) from exc
    except OSError as exc:
        log.error("write_io_error", path=str(resolved), error=str(exc), trace_id=trace_id)
        raise WriteError(
            "io_error",
            f"Writing {str(resolved)!r} failed: {exc}",
            retryable=True,
            next_action="Call write once more; if it fails again, tell the user.",
        ) from exc

    bytes_written = len(content.encode("utf-8"))
    log.info(
        "write_executor_success",
        path=str(resolved),
        bytes_written=bytes_written,
        mode=mode,
        unattended=unattended,
        trace_id=trace_id,
    )

    result: dict[str, Any] = {
        "path": str(resolved),
        "bytes_written": bytes_written,
        "mode": mode,
    }

    if not unattended:
        result["requires_approval_in_normal_mode"] = True

    return result
