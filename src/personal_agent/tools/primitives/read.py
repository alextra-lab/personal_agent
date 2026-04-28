"""Primitive read tool — supersedes ``read_file``.

Reads a file's content from the filesystem with explicit path-governance checks
(allowed_paths / forbidden_paths from ``config/governance/tools.yaml``).

FRE-261 Step 3.
"""

from pathlib import Path
from typing import Any

import structlog

from personal_agent.telemetry import TraceContext
from personal_agent.tools.primitives._governance import (
    _check_path_governance,
    _expand_path,
)
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------

read_tool = ToolDefinition(
    name="read",
    description=(
        "Read a file's content from the filesystem. "
        "Returns path, size in bytes, and content as a string."
    ),
    category="read_only",
    parameters=[
        ToolParameter(
            name="path",
            type="string",
            description="Absolute or home-relative path to the file",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="max_bytes",
            type="number",
            description="Maximum bytes to read (default 1 048 576)",
            required=False,
            default=1_048_576,
            json_schema=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=10,
    rate_limit_per_hour=None,
)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def read_executor(
    path: str,
    max_bytes: int = 1_048_576,
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """Execute the ``read`` primitive tool.

    Reads up to *max_bytes* bytes from the file at *path*, after applying
    path-governance checks from ``config/governance/tools.yaml``.

    Args:
        path: Absolute or home-relative path to the file.
        max_bytes: Maximum number of bytes to return (default 1 MiB).
        ctx: Optional trace context for structured logging correlation.

    Returns:
        On success::

            {
                "success": True,
                "path": str,
                "size_bytes": int,
                "content": str,
                "truncated": bool,
            }

        On failure::

            {"success": False, "error": "<error_code>", "path": str, ...}

        Possible ``error`` values:

        * ``"forbidden_path"`` — path matched a ``forbidden_paths`` entry
        * ``"path_not_allowed"`` — path not in ``allowed_paths``
        * ``"not_a_file"`` — path exists but is not a regular file
        * ``"too_large"`` — file size exceeds *max_bytes*
        * ``"permission_denied"`` — OS permission error
        * ``"io_error"`` — other I/O error
    """
    trace_id = ctx.trace_id if ctx else "n/a"

    # 1. Resolve path
    resolved = Path(_expand_path(path)).expanduser().resolve()
    log.debug("read_executor_called", path=path, resolved=str(resolved), trace_id=trace_id)

    # 2. Path governance
    governance_error = _check_path_governance(resolved, tool_name="read")
    if governance_error is not None:
        log.warning(
            "read_path_rejected",
            reason=governance_error.get("error"),
            path=str(resolved),
            trace_id=trace_id,
        )
        return governance_error

    # 3. Must be a regular file
    if not resolved.exists() or not resolved.is_file():
        return {
            "success": False,
            "error": "not_a_file",
            "path": str(resolved),
            "detail": f"Path {str(resolved)!r} is not an existing regular file",
        }

    # 4. Size check — stat first so we can reject huge files without reading them
    actual_size = resolved.stat().st_size
    if actual_size > max_bytes:
        return {
            "success": False,
            "error": "too_large",
            "path": str(resolved),
            "size_bytes": actual_size,
            "max_bytes": max_bytes,
            "detail": (f"File size {actual_size} bytes exceeds max_bytes limit {max_bytes}"),
        }

    # 5. Bounded read — read one extra byte to detect truncation without loading
    #    the full file into RAM when max_bytes < actual_size.
    try:
        with resolved.open("rb") as fh:
            raw = fh.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        content = raw[:max_bytes].decode("utf-8", errors="replace")
    except PermissionError as exc:
        log.warning("read_permission_denied", path=str(resolved), error=str(exc), trace_id=trace_id)
        return {
            "success": False,
            "error": "permission_denied",
            "path": str(resolved),
            "detail": str(exc),
        }
    except OSError as exc:
        log.error("read_io_error", path=str(resolved), error=str(exc), trace_id=trace_id)
        return {
            "success": False,
            "error": "io_error",
            "path": str(resolved),
            "detail": str(exc),
        }

    log.info(
        "read_executor_success",
        path=str(resolved),
        size_bytes=actual_size,
        truncated=truncated,
        trace_id=trace_id,
    )
    return {
        "success": True,
        "path": str(resolved),
        "size_bytes": actual_size,
        "content": content,
        "truncated": truncated,
    }
