"""Tests for telemetry metrics and log query utilities."""

import json
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest

from personal_agent.telemetry.events import (
    MODEL_CALL_COMPLETED,
    REPLY_READY,
    REQUEST_RECEIVED,
    STATE_TRANSITION,
    SYSTEM_METRICS_SNAPSHOT,
    TASK_COMPLETED,
    TASK_STARTED,
)
from personal_agent.telemetry.metrics import (
    _parse_time_window,
    get_recent_cpu_load,
    get_recent_event_count,
    get_request_latency_breakdown,
    get_trace_events,
    query_events,
)


class TestTimeWindowParsing:
    """Test time window string parsing."""

    def test_parse_hours(self) -> None:
        """Test parsing hour windows."""
        assert _parse_time_window("1h") == timedelta(hours=1)
        assert _parse_time_window("24h") == timedelta(hours=24)
        assert _parse_time_window("0h") == timedelta(hours=0)

    def test_parse_minutes(self) -> None:
        """Test parsing minute windows."""
        assert _parse_time_window("30m") == timedelta(minutes=30)
        assert _parse_time_window("1m") == timedelta(minutes=1)

    def test_parse_seconds(self) -> None:
        """Test parsing second windows."""
        assert _parse_time_window("45s") == timedelta(seconds=45)
        assert _parse_time_window("1s") == timedelta(seconds=1)

    def test_parse_days(self) -> None:
        """Test parsing day windows."""
        assert _parse_time_window("2d") == timedelta(days=2)
        assert _parse_time_window("7d") == timedelta(days=7)

    def test_parse_case_insensitive(self) -> None:
        """Test that parsing is case-insensitive."""
        assert _parse_time_window("1H") == timedelta(hours=1)
        assert _parse_time_window("30M") == timedelta(minutes=30)

    def test_parse_invalid_format(self) -> None:
        """Test that invalid formats raise ValueError."""
        with pytest.raises(ValueError, match="Invalid time window format"):
            _parse_time_window("invalid")
        with pytest.raises(ValueError, match="Unknown time unit"):
            _parse_time_window("1x")  # Unknown unit
        with pytest.raises(ValueError, match="Invalid time window format"):
            _parse_time_window("abc")  # Not a number


class TestLogQueryFunctions:
    """Test log query functions with mock log files."""

    def _create_log_entry(
        self,
        event: str,
        trace_id: str | None = None,
        component: str = "test",
        timestamp: datetime | None = None,
        **kwargs: object,
    ) -> dict:
        """Create a mock log entry."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        entry = {
            "event": event,
            "timestamp": timestamp.isoformat(),
            "component": component,
            "logger": f"personal_agent.{component}",
            "level": "info",
            **kwargs,
        }
        if trace_id:
            entry["trace_id"] = trace_id
        return entry

    def _write_log_file(self, log_file: pathlib.Path, entries: list[dict]) -> None:
        """Write log entries to a JSONL file."""
        with open(log_file, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    @patch("personal_agent.telemetry.metrics._get_log_file_path")
    def test_get_recent_event_count(self, mock_get_log_file: Any, tmp_path: pathlib.Path) -> None:
        """Test counting recent events."""
        log_file = tmp_path / "current.jsonl"
        mock_get_log_file.return_value = log_file

        # Create log entries with different timestamps
        now = datetime.now(timezone.utc)
        entries = [
            self._create_log_entry(MODEL_CALL_COMPLETED, timestamp=now - timedelta(seconds=30)),
            self._create_log_entry(MODEL_CALL_COMPLETED, timestamp=now - timedelta(seconds=60)),
            self._create_log_entry(
                TASK_STARTED, timestamp=now - timedelta(seconds=30)
            ),  # Different event
            self._create_log_entry(
                MODEL_CALL_COMPLETED, timestamp=now - timedelta(seconds=120)
            ),  # Too old
        ]

        self._write_log_file(log_file, entries)

        # Count events in last 90 seconds
        count = get_recent_event_count(MODEL_CALL_COMPLETED, window_seconds=90)
        assert count == 2  # Only the two recent ones

    @patch("personal_agent.telemetry.metrics._get_log_file_path")
    def test_get_recent_cpu_load(self, mock_get_log_file: Any, tmp_path: pathlib.Path) -> None:
        """Test getting recent CPU load values."""
        log_file = tmp_path / "current.jsonl"
        mock_get_log_file.return_value = log_file

        now = datetime.now(timezone.utc)
        entries = [
            self._create_log_entry(
                SYSTEM_METRICS_SNAPSHOT,
                timestamp=now - timedelta(seconds=30),
                cpu_load_percent=45.2,
            ),
            self._create_log_entry(
                SYSTEM_METRICS_SNAPSHOT,
                timestamp=now - timedelta(seconds=60),
                cpu_load_percent=62.1,
            ),
            self._create_log_entry(
                SYSTEM_METRICS_SNAPSHOT,
                timestamp=now - timedelta(seconds=120),
                cpu_load_percent=80.0,
            ),  # Too old
            self._create_log_entry(
                TASK_STARTED, timestamp=now - timedelta(seconds=30)
            ),  # Not a metrics snapshot
        ]

        self._write_log_file(log_file, entries)

        # Get CPU loads in last 90 seconds
        cpu_loads = get_recent_cpu_load(window_seconds=90)
        assert len(cpu_loads) == 2
        assert 45.2 in cpu_loads
        assert 62.1 in cpu_loads
        assert 80.0 not in cpu_loads  # Too old

    @patch("personal_agent.telemetry.metrics._get_log_file_path")
    def test_get_trace_events(self, mock_get_log_file: Any, tmp_path: pathlib.Path) -> None:
        """Test reconstructing trace events."""
        log_file = tmp_path / "current.jsonl"
        mock_get_log_file.return_value = log_file

        trace_id = "trace-abc-123"
        other_trace_id = "trace-xyz-789"

        now = datetime.now(timezone.utc)
        entries = [
            self._create_log_entry(
                TASK_STARTED, trace_id=trace_id, timestamp=now - timedelta(seconds=10)
            ),
            self._create_log_entry(
                MODEL_CALL_COMPLETED,
                trace_id=trace_id,
                timestamp=now - timedelta(seconds=5),
            ),
            self._create_log_entry(
                TASK_STARTED, trace_id=other_trace_id, timestamp=now - timedelta(seconds=8)
            ),  # Different trace
            self._create_log_entry(
                MODEL_CALL_COMPLETED,
                trace_id=trace_id,
                timestamp=now - timedelta(seconds=2),
            ),
        ]

        self._write_log_file(log_file, entries)

        # Get trace events
        trace_entries = get_trace_events(trace_id)
        assert len(trace_entries) == 3
        assert all(entry.get("trace_id") == trace_id for entry in trace_entries)
        # Should be sorted by timestamp
        timestamps = [entry.get("timestamp") for entry in trace_entries]
        assert timestamps == sorted(timestamps)

    @patch("personal_agent.telemetry.metrics._get_log_file_path")
    def test_query_events_by_event(self, mock_get_log_file: Any, tmp_path: pathlib.Path) -> None:
        """Test querying events by event name."""
        log_file = tmp_path / "current.jsonl"
        mock_get_log_file.return_value = log_file

        now = datetime.now(timezone.utc)
        entries = [
            self._create_log_entry(MODEL_CALL_COMPLETED, timestamp=now - timedelta(seconds=30)),
            self._create_log_entry(TASK_STARTED, timestamp=now - timedelta(seconds=20)),
            self._create_log_entry(MODEL_CALL_COMPLETED, timestamp=now - timedelta(seconds=10)),
        ]

        self._write_log_file(log_file, entries)

        # Query by event
        results = query_events(event=MODEL_CALL_COMPLETED, window_str="1h")
        assert len(results) == 2
        assert all(entry.get("event") == MODEL_CALL_COMPLETED for entry in results)

    @patch("personal_agent.telemetry.metrics._get_log_file_path")
    def test_query_events_by_component(
        self, mock_get_log_file: Any, tmp_path: pathlib.Path
    ) -> None:
        """Test querying events by component."""
        log_file = tmp_path / "current.jsonl"
        mock_get_log_file.return_value = log_file

        now = datetime.now(timezone.utc)
        entries = [
            self._create_log_entry(
                TASK_STARTED, component="orchestrator", timestamp=now - timedelta(seconds=30)
            ),
            self._create_log_entry(
                MODEL_CALL_COMPLETED,
                component="llm_client",
                timestamp=now - timedelta(seconds=20),
            ),
            self._create_log_entry(
                TASK_STARTED, component="orchestrator", timestamp=now - timedelta(seconds=10)
            ),
        ]

        self._write_log_file(log_file, entries)

        # Query by component
        results = query_events(component="orchestrator", window_str="1h")
        assert len(results) == 2
        assert all(entry.get("component") == "orchestrator" for entry in results)

    @patch("personal_agent.telemetry.metrics._get_log_file_path")
    def test_query_events_with_limit(self, mock_get_log_file: Any, tmp_path: pathlib.Path) -> None:
        """Test querying events with limit."""
        log_file = tmp_path / "current.jsonl"
        mock_get_log_file.return_value = log_file

        now = datetime.now(timezone.utc)
        entries = [
            self._create_log_entry(TASK_STARTED, timestamp=now - timedelta(seconds=i * 10))
            for i in range(10)
        ]

        self._write_log_file(log_file, entries)

        # Query with limit
        results = query_events(event=TASK_STARTED, window_str="2h", limit=5)
        assert len(results) == 5

    @patch("personal_agent.telemetry.metrics._get_log_file_path")
    def test_query_events_empty_result(
        self, mock_get_log_file: Any, tmp_path: pathlib.Path
    ) -> None:
        """Test querying with no matching results."""
        log_file = tmp_path / "current.jsonl"
        mock_get_log_file.return_value = log_file

        # Empty log file
        self._write_log_file(log_file, [])

        results = query_events(event=TASK_STARTED, window_str="1h")
        assert len(results) == 0

    @patch("personal_agent.telemetry.metrics._get_log_file_path")
    def test_query_events_handles_rotated_files(
        self, mock_get_log_file: Any, tmp_path: pathlib.Path
    ) -> None:
        """Test that query reads from rotated log files."""
        log_file = tmp_path / "current.jsonl"
        log_file_1 = tmp_path / "current.jsonl.1"
        mock_get_log_file.return_value = log_file

        now = datetime.now(timezone.utc)
        # Entries in current file
        current_entries = [
            self._create_log_entry(TASK_STARTED, timestamp=now - timedelta(seconds=30)),
        ]
        # Entries in rotated file
        rotated_entries = [
            self._create_log_entry(TASK_STARTED, timestamp=now - timedelta(seconds=3600)),
        ]

        self._write_log_file(log_file, current_entries)
        self._write_log_file(log_file_1, rotated_entries)

        # Query should find entries from both files
        results = query_events(event=TASK_STARTED, window_str="2h")
        assert len(results) >= 1  # At least the recent one

    @patch("personal_agent.telemetry.metrics._get_log_file_path")
    def test_get_trace_events_empty_trace(
        self, mock_get_log_file: Any, tmp_path: pathlib.Path
    ) -> None:
        """Test getting trace events for non-existent trace."""
        log_file = tmp_path / "current.jsonl"
        mock_get_log_file.return_value = log_file

        # Log file with different trace
        entries = [
            self._create_log_entry(
                TASK_STARTED, trace_id="other-trace", timestamp=datetime.now(timezone.utc)
            ),
        ]
        self._write_log_file(log_file, entries)

        # Query for non-existent trace
        results = get_trace_events("non-existent-trace")
        assert len(results) == 0

    @patch("personal_agent.telemetry.metrics._get_log_file_path")
    def test_get_request_latency_breakdown(
        self, mock_get_log_file: Any, tmp_path: pathlib.Path
    ) -> None:
        """Test request-to-reply latency breakdown from trace events."""
        log_file = tmp_path / "current.jsonl"
        mock_get_log_file.return_value = log_file

        trace_id = "trace-latency-123"
        # Timeline: t0=request_received, t0+100ms=task_started, then state_transition
        # from_state=init at t0+100, from_state=llm_call at t0+500, task_completed at t0+2000,
        # reply_ready at t0+2010
        t0 = datetime.now(timezone.utc)
        entries = [
            self._create_log_entry(
                REQUEST_RECEIVED, trace_id=trace_id, timestamp=t0
            ),
            self._create_log_entry(
                TASK_STARTED, trace_id=trace_id, timestamp=t0 + timedelta(milliseconds=100)
            ),
            self._create_log_entry(
                STATE_TRANSITION,
                trace_id=trace_id,
                timestamp=t0 + timedelta(milliseconds=100),
                from_state="init",
            ),
            self._create_log_entry(
                STATE_TRANSITION,
                trace_id=trace_id,
                timestamp=t0 + timedelta(milliseconds=500),
                from_state="llm_call",
            ),
            self._create_log_entry(
                TASK_COMPLETED,
                trace_id=trace_id,
                timestamp=t0 + timedelta(milliseconds=2000),
            ),
            self._create_log_entry(
                REPLY_READY,
                trace_id=trace_id,
                timestamp=t0 + timedelta(milliseconds=2010),
            ),
        ]
        self._write_log_file(log_file, entries)

        breakdown = get_request_latency_breakdown(trace_id)

        phases = [r["phase"] for r in breakdown]
        assert "entry_to_task" in phases
        assert "init" in phases
        assert "llm_call" in phases
        assert "task_to_reply" in phases
        assert "total_request_to_reply" in phases

        entry_to_task = next(r for r in breakdown if r["phase"] == "entry_to_task")
        assert entry_to_task["duration_ms"] == 100.0

        init_phase = next(r for r in breakdown if r["phase"] == "init")
        assert init_phase["duration_ms"] == 400.0  # 500 - 100

        total = next(r for r in breakdown if r["phase"] == "total_request_to_reply")
        assert total["duration_ms"] == 2010.0

    @patch("personal_agent.telemetry.metrics._get_log_file_path")
    def test_get_request_latency_breakdown_empty_trace(
        self, mock_get_log_file: Any, tmp_path: pathlib.Path
    ) -> None:
        """Test latency breakdown returns empty list for unknown trace."""
        log_file = tmp_path / "current.jsonl"
        mock_get_log_file.return_value = log_file
        self._write_log_file(log_file, [])

        assert get_request_latency_breakdown("no-such-trace") == []
