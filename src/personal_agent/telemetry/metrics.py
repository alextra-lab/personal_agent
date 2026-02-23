"""Telemetry metrics and log query utilities.

This module provides functions to query and analyze telemetry logs for metrics
and trace reconstruction. All queries operate on the JSONL log files.
"""

import json
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any

from personal_agent.telemetry import get_logger

log = get_logger(__name__)


def _get_log_file_path() -> pathlib.Path:
    """Get the path to the current log file.

    Returns:
        Path to current.jsonl log file.
    """
    # Lazy import to avoid circular dependency with config module
    from personal_agent.config import settings

    # Use settings.log_dir which is a Path object
    log_dir = pathlib.Path(str(settings.log_dir))
    return log_dir / "current.jsonl"


def _parse_time_window(window_str: str) -> timedelta:
    """Parse a time window string into a timedelta.

    Supports formats like:
    - "1h" -> 1 hour
    - "30m" -> 30 minutes
    - "2d" -> 2 days
    - "45s" -> 45 seconds

    Args:
        window_str: Time window string (e.g., "1h", "30m").

    Returns:
        Timedelta object.

    Raises:
        ValueError: If format is invalid.
    """
    if not window_str:
        raise ValueError("Time window string cannot be empty")

    # Extract number and unit
    window_str = window_str.strip().lower()
    if not window_str[-1].isalpha():
        raise ValueError(f"Invalid time window format: {window_str}")

    unit = window_str[-1]
    try:
        value = int(window_str[:-1])
    except ValueError as e:
        raise ValueError(f"Invalid time window format: {window_str}") from e

    # Map unit to timedelta
    unit_map = {
        "s": timedelta(seconds=1),
        "m": timedelta(minutes=1),
        "h": timedelta(hours=1),
        "d": timedelta(days=1),
    }

    if unit not in unit_map:
        raise ValueError(f"Unknown time unit: {unit}. Supported: s, m, h, d")

    return value * unit_map[unit]


def _read_log_entries(
    start_time: datetime | None = None, end_time: datetime | None = None
) -> list[dict[str, Any]]:
    """Read log entries from the log file, optionally filtered by time.

    Also reads from rotated log files (current.jsonl.1, current.jsonl.2, etc.)

    Args:
        start_time: Optional start time filter (inclusive).
        end_time: Optional end time filter (inclusive).

    Returns:
        List of parsed log entries (dicts).
    """
    log_file = _get_log_file_path()
    log_dir = log_file.parent
    entries: list[dict[str, Any]] = []

    # Read from current log file and rotated backups
    # RotatingFileHandler creates backups: current.jsonl.1, current.jsonl.2, etc.
    log_files = [log_file]
    backup_index = 1
    while True:
        backup_file = log_dir / f"{log_file.name}.{backup_index}"
        if backup_file.exists():
            log_files.append(backup_file)
            backup_index += 1
        else:
            break

    # Read entries from all log files (oldest first)
    for log_file_path in reversed(log_files):
        if not log_file_path.exists():
            continue

        try:
            with open(log_file_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning(
                            "invalid_json_line",
                            file=str(log_file_path),
                            line=line[:100],
                        )
                        continue

                    # Parse timestamp
                    timestamp_str = entry.get("timestamp")
                    if not timestamp_str:
                        continue

                    try:
                        # Handle ISO format timestamps
                        if "T" in timestamp_str:
                            entry_time = datetime.fromisoformat(
                                timestamp_str.replace("Z", "+00:00")
                            )
                        else:
                            # Fallback for other formats
                            entry_time = datetime.fromtimestamp(
                                float(timestamp_str), tz=timezone.utc
                            )
                    except (ValueError, TypeError):
                        log.warning(
                            "invalid_timestamp",
                            file=str(log_file_path),
                            timestamp=timestamp_str,
                        )
                        continue

                    # Apply time filters
                    if start_time and entry_time < start_time:
                        continue
                    if end_time and entry_time > end_time:
                        continue

                    entries.append(entry)

        except OSError as e:
            log.warning("failed_to_read_log_file", file=str(log_file_path), error=str(e))
            continue

    return entries


def get_recent_event_count(event: str, window_seconds: int) -> int:
    """Count occurrences of a specific event in the recent time window.

    Args:
        event: Event name to count (e.g., "model_call_completed").
        window_seconds: Time window in seconds.

    Returns:
        Count of events matching the criteria.
    """
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(seconds=window_seconds)

    entries = _read_log_entries(start_time=start_time, end_time=end_time)

    count = sum(1 for entry in entries if entry.get("event") == event)
    return count


def get_recent_cpu_load(window_seconds: int) -> list[float]:
    """Get recent CPU load values from system_metrics_snapshot events.

    Args:
        window_seconds: Time window in seconds.

    Returns:
        List of CPU load percentages (0-100).
    """
    from personal_agent.telemetry.events import SYSTEM_METRICS_SNAPSHOT

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(seconds=window_seconds)

    entries = _read_log_entries(start_time=start_time, end_time=end_time)

    cpu_loads: list[float] = []
    for entry in entries:
        if entry.get("event") != SYSTEM_METRICS_SNAPSHOT:
            continue

        # Extract CPU load from various possible field names
        cpu_load = entry.get("cpu_load_percent") or entry.get("cpu_load")
        if cpu_load is not None:
            try:
                cpu_loads.append(float(cpu_load))
            except (ValueError, TypeError):
                continue

    return cpu_loads


def _parse_ts(entry: dict[str, Any]) -> datetime | None:
    """Parse timestamp from a log entry. Returns None if missing or invalid."""
    ts = entry.get("timestamp")
    if not ts:
        return None
    try:
        if isinstance(ts, datetime):
            return ts
        if isinstance(ts, str):
            # ISO format with optional Z or +00:00
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        pass
    return None


def get_request_latency_breakdown(trace_id: str) -> list[dict[str, Any]]:
    """Compute request-to-reply latency breakdown from trace logs.

    Uses events: request_received, task_started, state_transition (from_state),
    task_completed/task_failed, reply_ready to build a phased timeline. Each
    phase has start_time, end_time, and duration_ms so you can see what took
    the most time.

    Args:
        trace_id: Trace identifier (from a completed request).

    Returns:
        List of phase dicts with keys: phase, start_time (ISO str), end_time
        (ISO str or None for point events), duration_ms (float or None for
        point events). Phases: entry_to_task, init, planning, llm_call,
        tool_execution, synthesis, task_to_reply, and optionally request_received
        / reply_ready as markers.
    """
    from personal_agent.telemetry.events import (
        REPLY_READY,
        REQUEST_RECEIVED,
        TASK_COMPLETED,
        TASK_FAILED,
        TASK_STARTED,
        STATE_TRANSITION,
    )

    entries = get_trace_events(trace_id)
    if not entries:
        return []

    breakdown: list[dict[str, Any]] = []

    def ts_str(d: datetime | None) -> str:
        return d.isoformat() if d else ""

    # Find key events (first occurrence each)
    request_ts: datetime | None = None
    task_start_ts: datetime | None = None
    task_end_ts: datetime | None = None
    reply_ts: datetime | None = None
    state_starts: list[tuple[str, datetime]] = []  # (from_state, ts)

    for e in entries:
        ev = e.get("event")
        t = _parse_ts(e)
        if not t:
            continue
        if ev == REQUEST_RECEIVED and request_ts is None:
            request_ts = t
        elif ev == TASK_STARTED and task_start_ts is None:
            task_start_ts = t
        elif ev == STATE_TRANSITION:
            from_state = e.get("from_state")
            if from_state:
                state_starts.append((str(from_state), t))
        elif ev in (TASK_COMPLETED, TASK_FAILED) and task_end_ts is None:
            task_end_ts = t
        elif ev == REPLY_READY and reply_ts is None:
            reply_ts = t

    # Build entry_to_task phase (request_received -> task_started)
    if request_ts and task_start_ts:
        dur = (task_start_ts - request_ts).total_seconds() * 1000
        breakdown.append(
            {
                "phase": "entry_to_task",
                "start_time": ts_str(request_ts),
                "end_time": ts_str(task_start_ts),
                "duration_ms": round(dur, 2),
                "description": "Request received until task started (session/mode setup)",
            }
        )
    elif request_ts:
        breakdown.append(
            {
                "phase": "request_received",
                "start_time": ts_str(request_ts),
                "end_time": None,
                "duration_ms": None,
                "description": "Request received (no task_started in trace)",
            }
        )

    # Build per-state phases from consecutive state_transition events
    for i, (from_state, start_dt) in enumerate(state_starts):
        end_dt: datetime | None = None
        if i + 1 < len(state_starts):
            end_dt = state_starts[i + 1][1]
        elif task_end_ts:
            end_dt = task_end_ts
        if end_dt is not None:
            dur = (end_dt - start_dt).total_seconds() * 1000
            breakdown.append(
                {
                    "phase": from_state,
                    "start_time": ts_str(start_dt),
                    "end_time": ts_str(end_dt),
                    "duration_ms": round(dur, 2),
                    "description": f"State: {from_state}",
                }
            )
        else:
            breakdown.append(
                {
                    "phase": from_state,
                    "start_time": ts_str(start_dt),
                    "end_time": None,
                    "duration_ms": None,
                    "description": f"State: {from_state} (in progress or no end event)",
                }
            )

    # task_to_reply: task_completed -> reply_ready
    if task_end_ts and reply_ts:
        dur = (reply_ts - task_end_ts).total_seconds() * 1000
        breakdown.append(
            {
                "phase": "task_to_reply",
                "start_time": ts_str(task_end_ts),
                "end_time": ts_str(reply_ts),
                "duration_ms": round(dur, 2),
                "description": "Task ended until reply ready (result build)",
            }
        )

    # Total: request_received -> reply_ready
    if request_ts and reply_ts:
        total_ms = (reply_ts - request_ts).total_seconds() * 1000
        breakdown.append(
            {
                "phase": "total_request_to_reply",
                "start_time": ts_str(request_ts),
                "end_time": ts_str(reply_ts),
                "duration_ms": round(total_ms, 2),
                "description": "Total: request received to reply ready",
            }
        )

    return breakdown


def get_trace_events(trace_id: str) -> list[dict[str, Any]]:
    """Reconstruct all log entries for a given trace_id.

    Args:
        trace_id: Trace identifier to reconstruct.

    Returns:
        List of log entries (dicts) for the trace, ordered by timestamp.
    """
    entries = _read_log_entries()

    # Filter entries by trace_id
    trace_entries = [entry for entry in entries if entry.get("trace_id") == trace_id]

    # Sort by timestamp
    trace_entries.sort(
        key=lambda e: e.get("timestamp", ""),
    )

    return trace_entries


def query_events(
    event: str | None = None,
    window_str: str | None = None,
    component: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Query log entries with flexible filters.

    Args:
        event: Optional event name filter.
        window_str: Optional time window (e.g., "1h", "30m").
        component: Optional component name filter.
        limit: Optional maximum number of results.

    Returns:
        List of matching log entries, ordered by timestamp (newest first).
    """
    # Parse time window if provided
    start_time: datetime | None = None
    if window_str:
        window_delta = _parse_time_window(window_str)
        start_time = datetime.now(timezone.utc) - window_delta

    entries = _read_log_entries(start_time=start_time)

    # Apply filters
    filtered = []
    for entry in entries:
        if event and entry.get("event") != event:
            continue
        if component and entry.get("component") != component:
            continue
        filtered.append(entry)

    # Sort by timestamp (newest first)
    filtered.sort(
        key=lambda e: e.get("timestamp", ""),
        reverse=True,
    )

    # Apply limit
    if limit:
        filtered = filtered[:limit]

    return filtered
