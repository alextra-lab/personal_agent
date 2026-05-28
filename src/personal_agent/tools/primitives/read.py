"""Primitive read tool — supersedes ``read_file``.

Reads a file's content from the filesystem with explicit path-governance checks
(allowed_paths / forbidden_paths from ``config/governance/tools.yaml``).

By default the tool returns a *truncated head* (first ``DEFAULT_LINE_LIMIT`` lines, capped at
``DEFAULT_HEAD_BYTES``) so a single read never floods the context window. Callers page through
larger files with ``offset``/``limit``, read the end of growing logs with ``tail_lines``, or opt
into a deliberate large read with an explicit ``max_bytes``.

Internal callers (orchestrator, brainstem, Captain's Log) that need a known slice should pass
explicit ``offset``/``limit`` rather than relying on the head default, and should prefer a grep
(via the ``bash`` primitive) to locate the relevant range before reading.

FRE-261 Step 3. FRE-355: tail_lines parameter for large log files.
FRE-410: per-read head cap + line-based offset/limit to stop whole-file context bloat.
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
# Defaults (FRE-410)
# ---------------------------------------------------------------------------

DEFAULT_LINE_LIMIT = 200  # default head: first N lines
DEFAULT_HEAD_BYTES = 8_192  # default head byte cap (~2K tokens)
LEGACY_TAIL_CAP = 1_048_576  # tail mode keeps the old 1 MiB cap (no log regression)

# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------

read_tool = ToolDefinition(
    name="read",
    description=(
        "Read a file's content from the filesystem. By default returns a truncated head "
        "(first ~200 lines / ~8 KB) with truncated=true and a 'marker' explaining how to get "
        "more: page with offset/limit, or run grep via the bash tool to jump straight to the "
        "relevant section (cheaper than reading the whole file). "
        "Use offset (1-based line number) and limit (number of lines) to read a specific range. "
        "Use tail_lines to read the last N lines of a growing log file. "
        "Set max_bytes only for a deliberate large read when you truly need the whole file."
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
            name="offset",
            type="number",
            description=(
                "1-based line number to start reading from (default 1). "
                "Use the value from a previous read's marker to page through a large file."
            ),
            required=False,
            default=1,
            json_schema=None,
        ),
        ToolParameter(
            name="limit",
            type="number",
            description=(
                "Number of lines to return (default 200). Combined with offset to read a range."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="max_bytes",
            type="number",
            description=(
                "Explicit byte cap on returned content. Defaults to ~8 KB in normal (head/range) "
                "mode and 1 MiB in tail_lines mode. Set a larger value only for a deliberate "
                "large read."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="tail_lines",
            type="number",
            description=(
                "When set, return the last N lines of the file instead of reading from the start. "
                "Bypasses the head cap so large log files (e.g. current.jsonl) are accessible. "
                "Output is still capped at max_bytes bytes of text (default 1 MiB in tail mode)."
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


def _read_line_window(
    fh: IO[bytes], offset: int, limit: int, byte_cap: int
) -> tuple[str, bool, int, int, bool]:
    """Return ``(content, truncated, lines_returned, total_lines, line_clipped)`` for a window.

    Reads the 1-based line window ``[offset, offset + limit)``. Streams the file counting every
    line so ``total_lines`` is exact, while retaining only the windowed lines. The byte cap is
    enforced on whole-line boundaries: a line that would push the output past ``byte_cap`` is
    excluded so the continuation offset can re-read it intact. The sole exception is a single
    line larger than ``byte_cap`` on its own, which is clipped mid-line to guarantee forward
    progress; ``line_clipped`` is then True so the caller can tell the reader the clipped line's
    remainder is only reachable with a larger ``max_bytes``.

    Args:
        fh: Open binary file handle positioned at the start.
        offset: 1-based line number to start from (clamped to >= 1).
        limit: Maximum number of lines to consider for the window (expected >= 1).
        byte_cap: Maximum number of bytes of decoded content to return.

    Returns:
        A tuple of the decoded content, whether the window was truncated (more lines follow or a
        byte clip occurred), the number of lines actually returned, the file's total line count,
        and whether a single oversized line was clipped mid-line.
    """
    start = max(offset, 1)
    end = start + limit  # exclusive, 1-based
    window: list[bytes] = []
    total = 0
    for i, raw_line in enumerate(fh, start=1):
        total = i
        if start <= i < end:
            window.append(raw_line)

    out: list[bytes] = []
    used = 0
    byte_clipped = False
    line_clipped = False
    for raw_line in window:
        if used + len(raw_line) <= byte_cap:
            out.append(raw_line)
            used += len(raw_line)
        else:
            if not out:  # single line larger than the cap: clip to make progress
                out.append(raw_line[:byte_cap])
                line_clipped = True
            byte_clipped = True
            break

    content_bytes = b"".join(out)
    lines_returned = len(out)
    more_lines = total > (start - 1) + lines_returned
    truncated = byte_clipped or more_lines
    return (
        content_bytes.decode("utf-8", errors="replace"),
        truncated,
        lines_returned,
        total,
        line_clipped,
    )


def _build_read_marker(start: int, lines_returned: int, total: int, line_clipped: bool) -> str:
    """Build the continuation marker shown when a head/range read is truncated.

    For a clean whole-line truncation the continuation offset is ``start + lines_returned`` — the
    first line not fully returned — so paging re-reads any whole line the byte cap excluded. When
    a single oversized line was clipped mid-line (``line_clipped``), that one line cannot be
    completed by paging, so the marker instead tells the reader to raise ``max_bytes`` (or grep
    within the line).

    Args:
        start: 1-based line number the window started at.
        lines_returned: Number of lines actually returned in this window.
        total: Total number of lines in the file.
        line_clipped: Whether a single oversized line was clipped mid-line.

    Returns:
        A human/LLM-readable marker string nudging offset-paging, a larger ``max_bytes``, or a
        grep-first lookup.
    """
    next_offset = start + (lines_returned if lines_returned else 1)
    last = next_offset - 1
    if line_clipped:
        return (
            f"Line {start} of {total} exceeds the byte cap and was clipped mid-line. "
            f"Pass a larger max_bytes to read it in full, or grep within it "
            f"(bash: grep -n <pattern> <path>). Pass offset={next_offset} to skip to the next line."
        )
    return (
        f"Showing lines {start}-{last} of {total}. "
        f"Pass offset={next_offset} to continue, or grep the file first "
        f"(bash: grep -n <pattern> <path>) to jump to the relevant section."
    )


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def read_executor(
    path: str,
    max_bytes: int | None = None,
    tail_lines: int | None = None,
    offset: int = 1,
    limit: int | None = None,
    *,
    ctx: TraceContext,
) -> dict[str, Any]:
    """Execute the ``read`` primitive tool.

    By default returns a *truncated head* of the file at *path* — the first
    ``DEFAULT_LINE_LIMIT`` lines, capped at ``DEFAULT_HEAD_BYTES`` bytes — after applying
    path-governance checks from ``config/governance/tools.yaml``. When the head is truncated the
    result carries ``truncated=True`` and a ``marker`` string explaining how to page further
    (via *offset*/*limit*) or grep first.

    *offset* (1-based line) and *limit* (line count) read a specific line range. *tail_lines*
    returns the last N lines of a growing log file and takes precedence over *offset*/*limit*.
    An explicit *max_bytes* overrides the default byte cap for a deliberate large read.

    Args:
        path: Absolute or home-relative path to the file.
        max_bytes: Explicit byte cap on returned content. Defaults to ``DEFAULT_HEAD_BYTES`` in
            head/range mode and ``LEGACY_TAIL_CAP`` (1 MiB) in tail mode.
        tail_lines: When set, return the last N lines of the file (takes precedence).
        offset: 1-based line number to start from in head/range mode (default 1).
        limit: Number of lines to return in head/range mode (default
            ``DEFAULT_LINE_LIMIT``).
        ctx: Trace context for structured logging correlation.

    Returns:
        On success in head/range mode::

            {
                "success": True,
                "path": str,
                "size_bytes": int,
                "content": str,
                "truncated": bool,
                "offset": int,
                "limit": int,
                "lines_returned": int,
                "total_lines": int,
                "marker": str | None,  # present (str) only when truncated
            }

        Tail mode additionally echoes ``"tail_lines"`` and omits the line-range fields.

        On failure::

            {"success": False, "error": "<error_code>", "path": str, ...}

        Possible ``error`` values:

        * ``"forbidden_path"`` — path matched a ``forbidden_paths`` entry
        * ``"path_not_allowed"`` — path not in ``allowed_paths``
        * ``"not_a_file"`` — path exists but is not a regular file
        * ``"permission_denied"`` — OS permission error
        * ``"io_error"`` — other I/O error
    """
    trace_id = ctx.trace_id
    session_id = ctx.session_id

    # 1. Resolve path
    resolved = Path(_expand_path(path)).expanduser().resolve()
    log.debug(
        "read_executor_called",
        path=path,
        resolved=str(resolved),
        trace_id=trace_id,
        session_id=session_id,
    )

    # 2. Path governance
    governance_error = _check_path_governance(resolved, tool_name="read", trace_id=trace_id)
    if governance_error is not None:
        log.warning(
            "read_path_rejected",
            reason=governance_error.get("error"),
            path=str(resolved),
            trace_id=trace_id,
            session_id=session_id,
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

    # 4a. Tail mode — read from EOF; keep the legacy 1 MiB cap unless overridden.
    if tail_lines is not None:
        tail_cap = max_bytes if max_bytes is not None else LEGACY_TAIL_CAP
        try:
            with resolved.open("rb") as fh:
                content, truncated = _read_tail(fh, tail_lines, tail_cap)
        except PermissionError as exc:
            log.warning(
                "read_permission_denied",
                path=str(resolved),
                error=str(exc),
                trace_id=trace_id,
                session_id=session_id,
            )
            return {
                "success": False,
                "error": "permission_denied",
                "path": str(resolved),
                "detail": str(exc),
            }
        except OSError as exc:
            log.error(
                "read_io_error",
                path=str(resolved),
                error=str(exc),
                trace_id=trace_id,
                session_id=session_id,
            )
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
            session_id=session_id,
        )
        return {
            "success": True,
            "path": str(resolved),
            "size_bytes": actual_size,
            "content": content,
            "truncated": truncated,
            "tail_lines": tail_lines,
        }

    # 4b. Head/range mode — truncate to a line window, never error on size.
    requested_limit = limit if limit is not None else DEFAULT_LINE_LIMIT
    effective_limit = max(requested_limit, 1)  # a non-positive limit would skip content
    byte_cap = max_bytes if max_bytes is not None else DEFAULT_HEAD_BYTES
    try:
        with resolved.open("rb") as fh:
            content, truncated, lines_returned, total_lines, line_clipped = _read_line_window(
                fh, offset, effective_limit, byte_cap
            )
    except PermissionError as exc:
        log.warning(
            "read_permission_denied",
            path=str(resolved),
            error=str(exc),
            trace_id=trace_id,
            session_id=session_id,
        )
        return {
            "success": False,
            "error": "permission_denied",
            "path": str(resolved),
            "detail": str(exc),
        }
    except OSError as exc:
        log.error(
            "read_io_error",
            path=str(resolved),
            error=str(exc),
            trace_id=trace_id,
            session_id=session_id,
        )
        return {
            "success": False,
            "error": "io_error",
            "path": str(resolved),
            "detail": str(exc),
        }

    start = max(offset, 1)
    marker = (
        _build_read_marker(start, lines_returned, total_lines, line_clipped) if truncated else None
    )

    log.info(
        "read_executor_success",
        path=str(resolved),
        size_bytes=actual_size,
        offset=start,
        limit=effective_limit,
        lines_returned=lines_returned,
        total_lines=total_lines,
        truncated=truncated,
        trace_id=trace_id,
        session_id=session_id,
    )
    return {
        "success": True,
        "path": str(resolved),
        "size_bytes": actual_size,
        "content": content,
        "truncated": truncated,
        "offset": start,
        "limit": effective_limit,
        "lines_returned": lines_returned,
        "total_lines": total_lines,
        "marker": marker,
    }
