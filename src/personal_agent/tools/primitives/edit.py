"""Primitive edit tool — replace one exact string in one file (ADR-0156 D7.1).

Zero matches or more than one match is an error that gives the count (S7, Anthropic's
``text_editor_20250728``). The path governance is ``write``'s: the ``edit`` row in
``config/governance/tools.yaml`` takes its lists from the ``write`` row.

FRE-1576.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal

import structlog

from personal_agent.telemetry import TraceContext
from personal_agent.tools.contract import ToolError, error_codes
from personal_agent.tools.primitives._governance import (
    PathCode,
    _check_durability,
    _check_path_governance,
    _expand_path,
)
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Failure codes (ADR-0156 D6.1)
# ---------------------------------------------------------------------------

EditCode = Literal[
    PathCode,
    "empty_old_string",
    "file_not_found",
    "not_a_file",
    "not_text",
    "no_match",
    "multiple_matches",
    "permission_denied",
    "io_error",
]


class EditError(ToolError[EditCode]):
    """An ``edit`` failure."""


# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------

edit_tool = ToolDefinition(
    name="edit",
    description=(
        "Replace one exact string in an existing file with new text. "
        "Read the file first and copy old_string exactly, with its whitespace and line "
        "breaks; it must occur exactly once in the file. "
        "Zero matches or more than one match is an error that gives the count and changes "
        "nothing: on more than one, add surrounding lines to old_string until it is unique. "
        "Do not use it to create a file or to rewrite a whole file (use write). "
        "Durable paths survive a restart: /app/agent_workspace/** and /app/telemetry/**; any "
        "other path under /app/ is refused, and a path outside the allowed paths is refused. "
        "It returns the line where the new text starts, so you can read that range back. "
        'Examples: edit(path="/app/agent_workspace/notes/plan.md", old_string="## Step 2\\n'
        'Draft", new_string="## Step 2\\nDone"), edit(path="/tmp/config.yaml", '
        'old_string="retries: 3", new_string="retries: 5").'
    ),
    category="system_write",
    parameters=[
        ToolParameter(
            name="path",
            type="string",
            description="Absolute path of the file to change",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="old_string",
            type="string",
            description="The exact text to replace. It must occur exactly once in the file",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="new_string",
            type="string",
            description="The text that replaces old_string",
            required=True,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="high",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "RECOVERY"],
    requires_approval=False,  # approval is via the permission rules (ADR-0156 D8)
    requires_sandbox=False,
    timeout_seconds=10,
    rate_limit_per_hour=None,
    error_codes=error_codes(EditError),
    max_result_tokens=1_000,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _match_offsets(text: str, old: str, limit: int = 2) -> list[int]:
    """Find where ``old`` starts in ``text``, overlapping matches included.

    ``str.count`` skips overlapping matches ("aa" in "aaa" counts once), which would hide an
    ambiguous edit.

    Args:
        text: The file's text.
        old: The non-empty string to find.
        limit: Stop after this many matches; only 0, 1 or "more than 1" matters.

    Returns:
        Up to ``limit`` start offsets.
    """
    offsets: list[int] = []
    start = text.find(old)
    while start != -1 and len(offsets) < limit:
        offsets.append(start)
        start = text.find(old, start + 1)
    return offsets


def _count_matches(text: str, old: str) -> int:
    """Count every start of ``old`` in ``text``, overlapping matches included."""
    return len(_match_offsets(text, old, limit=len(text) + 1))


def _replace_file(target: Path, data: bytes) -> None:
    """Replace ``target``'s content in one step, keeping its permission bits.

    The new content goes to a temporary file in the same directory, which then replaces the
    target, so a failed write never leaves the file half written.

    Args:
        target: The file to replace.
        data: Its new content.
    """
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".edit")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        shutil.copymode(target, tmp_name)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def edit_executor(
    path: str,
    old_string: str,
    new_string: str,
    *,
    ctx: TraceContext,
) -> dict[str, object]:
    """Execute the ``edit`` primitive tool.

    Every check runs before the write, so a refused edit leaves the file unchanged. Keep this
    function free of ``await`` between the read and the replace: two ``edit`` calls in one
    round then cannot interleave, and one cannot overwrite the other's change.

    Args:
        path: Absolute path of the file to change.
        old_string: The exact text to replace. It must occur exactly once.
        new_string: The replacement text.
        ctx: Trace context for structured logging correlation.

    Returns:
        ``{"path": str, "line": int, "bytes_written": int}``. ``line`` is the 1-based line
        where the new text starts.

    Raises:
        PathRefusalError: The path rules or the durability check refuse the path.
        EditError: Any other failure, with its code.
    """
    trace_id = ctx.trace_id

    if not old_string:
        raise EditError(
            "empty_old_string",
            "old_string is empty; an empty string matches everywhere.",
            retryable=True,
            next_action="Call edit with the exact text to replace, or use write for a new file.",
        )

    resolved = Path(_expand_path(path)).expanduser().resolve()
    log.debug("edit_executor_called", path=path, resolved=str(resolved), trace_id=trace_id)

    _check_path_governance(resolved, tool_name="edit", trace_id=trace_id)
    _check_durability(resolved, tool_name="edit", trace_id=trace_id)

    if not resolved.exists():
        raise EditError(
            "file_not_found",
            f"{str(resolved)!r} does not exist.",
            retryable=False,
            next_action="Check the path, or create the file with write.",
        )
    if not resolved.is_file():
        raise EditError(
            "not_a_file",
            f"{str(resolved)!r} is not a regular file.",
            retryable=False,
            next_action="Call edit with the path of a file, not a directory.",
        )

    try:
        raw = resolved.read_bytes()
    except PermissionError as exc:
        raise EditError(
            "permission_denied",
            f"Cannot read {str(resolved)!r}: {exc}",
            retryable=False,
            next_action="Do not retry this path. Edit a file under /app/agent_workspace/ or /tmp/.",
        ) from exc
    try:
        # Bytes, not read_text: read_text would turn \r\n into \n and change every line.
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EditError(
            "not_text",
            f"{str(resolved)!r} is not UTF-8 text.",
            retryable=False,
            next_action="Do not edit this file with edit. Rewrite it with write if needed.",
        ) from exc

    offsets = _match_offsets(text, old_string)
    if not offsets:
        raise EditError(
            "no_match",
            f"old_string matches 0 times in {str(resolved)!r}; it must match exactly once.",
            retryable=True,
            next_action=(
                "Read the file again and copy old_string exactly, with its whitespace and "
                "line breaks."
            ),
        )
    if len(offsets) > 1:
        count = _count_matches(text, old_string)
        raise EditError(
            "multiple_matches",
            f"old_string matches {count} times in {str(resolved)!r}; it must match exactly once.",
            retryable=True,
            next_action="Add surrounding lines to old_string until it matches exactly once.",
        )

    start = offsets[0]
    updated = text[:start] + new_string + text[start + len(old_string) :]
    data = updated.encode("utf-8")
    try:
        _replace_file(resolved, data)
    except PermissionError as exc:
        log.warning("edit_permission_denied", path=str(resolved), error=str(exc), trace_id=trace_id)
        raise EditError(
            "permission_denied",
            f"Cannot write {str(resolved)!r}: {exc}",
            retryable=False,
            next_action="Do not retry this path. Edit a file under /app/agent_workspace/ or /tmp/.",
        ) from exc
    except OSError as exc:
        log.error("edit_io_error", path=str(resolved), error=str(exc), trace_id=trace_id)
        raise EditError(
            "io_error",
            f"Writing {str(resolved)!r} failed: {exc}",
            retryable=True,
            next_action="Call edit once more; if it fails again, tell the user.",
        ) from exc

    line = text.count("\n", 0, start) + 1
    log.info(
        "edit_executor_success",
        path=str(resolved),
        line=line,
        bytes_written=len(data),
        trace_id=trace_id,
    )
    return {"path": str(resolved), "line": line, "bytes_written": len(data)}
