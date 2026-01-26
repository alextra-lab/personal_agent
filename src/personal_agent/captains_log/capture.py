"""Fast capture system for Captain's Log (Phase 2.2).

This module provides structured capture of task execution data without LLM processing.
Captures are written immediately during request processing, then processed later by
the second brain for deep reflection.
"""

import pathlib
from datetime import datetime, timezone
from typing import Any

import orjson
from pydantic import BaseModel, Field

from personal_agent.telemetry import get_logger

log = get_logger(__name__)


class TaskCapture(BaseModel):
    """Fast capture of task execution (no LLM, structured JSON).

    This is written immediately during request processing for later
    analysis by the second brain.
    """

    trace_id: str
    session_id: str
    timestamp: datetime
    user_message: str
    assistant_response: str | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    duration_ms: float | None = None
    metrics_summary: dict[str, Any] | None = None
    outcome: str  # "completed", "failed", "timeout"
    memory_context_used: bool = False
    memory_conversations_found: int = 0


def _get_captures_dir() -> pathlib.Path:
    """Get the captures directory path.

    Returns:
        Path to telemetry/captains_log/captures directory.
    """
    project_root = pathlib.Path(__file__).parent.parent.parent.parent
    captures_dir = project_root / "telemetry" / "captains_log" / "captures"
    captures_dir.mkdir(parents=True, exist_ok=True)
    return captures_dir


def write_capture(capture: TaskCapture) -> pathlib.Path:
    """Write a fast capture to disk (structured JSON, no LLM).

    Args:
        capture: Task capture to write

    Returns:
        Path to the written capture file
    """
    captures_dir = _get_captures_dir()

    # Organize by date: captures/YYYY-MM-DD/trace-id.json
    date_str = capture.timestamp.strftime("%Y-%m-%d")
    date_dir = captures_dir / date_str
    date_dir.mkdir(parents=True, exist_ok=True)

    # Filename: trace-id.json
    filename = f"{capture.trace_id}.json"
    file_path = date_dir / filename

    # Write JSON (pretty-printed with orjson for speed)
    json_content = orjson.dumps(
        capture.model_dump(),
        option=orjson.OPT_INDENT_2 | orjson.OPT_APPEND_NEWLINE,
    ).decode()
    file_path.write_text(json_content, encoding="utf-8")

    log.info(
        "capture_written",
        trace_id=capture.trace_id,
        file_path=str(file_path),
        outcome=capture.outcome,
    )

    return file_path


def read_captures(
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    limit: int = 100,
) -> list[TaskCapture]:
    """Read captures from disk.

    Args:
        start_date: Optional start date filter
        end_date: Optional end date filter
        limit: Maximum number of captures to return

    Returns:
        List of task captures
    """
    captures_dir = _get_captures_dir()
    captures: list[TaskCapture] = []

    if not captures_dir.exists():
        return captures

    # Iterate through date directories
    for date_dir in sorted(captures_dir.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue

        # Parse date from directory name
        try:
            dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if start_date and dir_date < start_date:
                continue
            if end_date and dir_date > end_date:
                continue
        except ValueError:
            continue

        # Read all JSON files in this date directory
        for json_file in date_dir.glob("*.json"):
            try:
                content = json_file.read_text(encoding="utf-8")
                data = orjson.loads(content)
                capture = TaskCapture(**data)
                captures.append(capture)

                if len(captures) >= limit:
                    return captures
            except Exception as e:
                log.warning(
                    "capture_read_failed",
                    file_path=str(json_file),
                    error=str(e),
                )

    return captures
