"""Primitive read tool — supersedes ``read_file``.

Reads a file's content from the filesystem with explicit path-governance checks
(allowed_paths / forbidden_paths from ``config/governance/tools.yaml``).

FRE-261 Step 3. FRE-355: tail_lines parameter for large log files.
"""

from pathlib import Path
from typing import IO, Any

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
        "Returns path, size in bytes, and content as a string. "
        "Use tail_lines to read the last N lines of large files (e.g. growing log files) "
        "without loading the entire file into memory."
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
            description="Maximum bytes to read (default 1 048 576). Ignored when tail_lines is set.",
            required=False,
            default=1_048_576,
            json_schema=None,
        ),
        ToolParameter(
            name="tail_lines",
            type="number",
            description=(
                "When set, return the last N lines of the file instead of reading from the start. "
                "Bypasses the max_bytes size cap so large log files (e.g. current.jsonl) are accessible. "
                "Output is still capped at max_bytes bytes of text."
            ),
            required=False,
            default=None,
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
# Internal helpers
# ---------------------------------------------------------------------------


def _read_tail(fh: IO[bytes], n: int, max_bytes: int) -> tuple[str, bool]:
    """Return (content, truncated) for the last *n* lines of an open binary file.

    Seeks backward in 4 KiB blocks until *n+1* newlines have been seen, then
    reconstructs the tail without loading the whole file into memory.
    """
    fh.seek(0, 2)  # seek to EOF
    remaining = fh.tell()

    chunks: list[bytes] = []
    newlines_seen = 0
    block_size = 4096

    while remaining > 0 and newlines_seen <= n:
        read_size = min(block_size, remaining)
        remaining -= read_size
        fh.seek(remaining)
        block = fh.read(read_size)
        chunks.append(block)
        newlines_seen += block.count(b"\n")

    content_bytes = b"".join(reversed(chunks))

    lines = content_bytes.split(b"\n")
    # Drop trailing empty element from a file that ends with a newline
    if lines and lines[-1] == b"":
        lines = lines[:-1]

    tail = lines[-n:] if len(lines) > n else lines
    tail_bytes = b"\n".join(tail)

    truncated = len(tail_bytes) > max_bytes
    if truncated:
        tail_bytes = tail_bytes[-max_bytes:]

    return tail_bytes.decode("utf-8", errors="replace"), truncated


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def read_executor(
    path: str,
    max_bytes: int = 1_048_576,
    tail_lines: int | None = None,
    *,
    ctx: TraceContext,
) -> dict[str, Any]:
    """Execute the ``read`` primitive tool.

    Reads up to *max_bytes* bytes from the file at *path*, after applying
    path-governance checks from ``config/governance/tools.yaml``.

    When *tail_lines* is set, the last *tail_lines* lines are returned instead
    of reading from the start. The ``max_bytes`` size-gate is bypassed so that
    large log files (e.g. ``telemetry/logs/current.jsonl``) are accessible;
    output is still capped at *max_bytes* bytes of text.

    Args:
        path: Absolute or home-relative path to the file.
        max_bytes: Maximum number of bytes to return (default 1 MiB).
            Ignored as a size-gate when *tail_lines* is set, but still used
            as the output cap for the tail content.
        tail_lines: When set, return the last N lines of the file.
        ctx: Trace context for structured logging correlation.

    Returns:
        On success::

            {
                "success": True,
                "path": str,
                "size_bytes": int,
                "content": str,
                "truncated": bool,
                "tail_lines": int | None,  # echoed when tail mode was used
            }

        On failure::

            {"success": False, "error": "<error_code>", "path": str, ...}

        Possible ``error`` values:

        * ``"forbidden_path"`` — path matched a ``forbidden_paths`` entry
        * ``"path_not_allowed"`` — path not in ``allowed_paths``
        * ``"not_a_file"`` — path exists but is not a regular file
        * ``"too_large"`` — file size exceeds *max_bytes* (normal mode only)
        * ``"permission_denied"`` — OS permission error
        * ``"io_error"`` — other I/O error
    """
    trace_id = ctx.trace_id

    # 1. Resolve path
    resolved = Path(_expand_path(path)).expanduser().resolve()
    log.debug("read_executor_called", path=path, resolved=str(resolved), trace_id=trace_id)

    # 2. Path governance
    governance_error = _check_path_governance(resolved, tool_name="read", trace_id=trace_id)
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

    actual_size = resolved.stat().st_size

    # 4a. Tail mode — bypass the size gate and read from EOF
    if tail_lines is not None:
        try:
            with resolved.open("rb") as fh:
                content, truncated = _read_tail(fh, tail_lines, max_bytes)
        except PermissionError as exc:
            log.warning(
                "read_permission_denied", path=str(resolved), error=str(exc), trace_id=trace_id
            )
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
            tail_lines=tail_lines,
            truncated=truncated,
            trace_id=trace_id,
        )
        return {
            "success": True,
            "path": str(resolved),
            "size_bytes": actual_size,
            "content": content,
            "truncated": truncated,
            "tail_lines": tail_lines,
        }

    # 4b. Normal mode — size gate applies
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
