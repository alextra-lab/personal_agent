"""Tests for self_telemetry_query tool."""

import json
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from personal_agent.tools.self_telemetry import (
    self_telemetry_query_executor,
    self_telemetry_query_tool,
)


def test_self_telemetry_tool_definition() -> None:
    """Tool definition should match governance and parameter requirements."""
    assert self_telemetry_query_tool.name == "self_telemetry_query"
    assert self_telemetry_query_tool.category == "read_only"
    assert self_telemetry_query_tool.risk_level == "low"
    assert set(self_telemetry_query_tool.allowed_modes) == {
        "NORMAL",
        "ALERT",
        "DEGRADED",
        "LOCKDOWN",
        "RECOVERY",
    }
    assert self_telemetry_query_tool.requires_approval is False
    assert self_telemetry_query_tool.requires_sandbox is False

    parameters = {p.name: p for p in self_telemetry_query_tool.parameters}
    assert "query_type" in parameters
    assert parameters["query_type"].required is True
    assert "trace_id" in parameters
    assert "event" in parameters
    assert "window" in parameters
    assert "component" in parameters
    assert "limit" in parameters


def test_events_query_dispatches_filters_and_default_limit() -> None:
    """Events query should call query_events with default limit of 20."""
    expected = [{"event": "model_call_completed"}]
    with patch(
        "personal_agent.tools.self_telemetry.query_events",
        return_value=expected,
    ) as mock_query_events:
        result = self_telemetry_query_executor(
            query_type="events",
            event="model_call_completed",
            window="1h",
            component="orchestrator",
        )

    mock_query_events.assert_called_once_with(
        event="model_call_completed",
        window_str="1h",
        component="orchestrator",
        limit=20,
    )
    assert result["success"] is True
    assert result["output"] == expected
    assert result["error"] is None


def test_trace_query_dispatches_for_valid_trace_id() -> None:
    """Trace query should return chronological trace events."""
    expected = [{"trace_id": "trace-123", "event": "request_received"}]
    with patch(
        "personal_agent.tools.self_telemetry.get_trace_events",
        return_value=expected,
    ) as mock_get_trace:
        result = self_telemetry_query_executor(
            query_type="trace",
            trace_id="trace-123",
        )

    mock_get_trace.assert_called_once_with("trace-123")
    assert result["success"] is True
    assert result["output"] == expected
    assert result["error"] is None


def test_latency_query_dispatches_for_valid_trace_id() -> None:
    """Latency query should return phase breakdown with duration_ms."""
    expected = [{"phase": "llm_call", "duration_ms": 123.45}]
    with patch(
        "personal_agent.tools.self_telemetry.get_request_latency_breakdown",
        return_value=expected,
    ) as mock_latency:
        result = self_telemetry_query_executor(
            query_type="latency",
            trace_id="trace-456",
        )

    mock_latency.assert_called_once_with("trace-456")
    assert result["success"] is True
    assert result["output"] == expected
    assert result["error"] is None


def test_trace_query_requires_trace_id() -> None:
    """Trace query should fail when trace_id is missing."""
    result = self_telemetry_query_executor(query_type="trace")
    assert result["success"] is False
    assert result["output"] == []
    assert "trace_id required" in (result["error"] or "")


def test_latency_query_requires_trace_id() -> None:
    """Latency query should fail when trace_id is missing."""
    result = self_telemetry_query_executor(query_type="latency")
    assert result["success"] is False
    assert result["output"] == []
    assert "trace_id required" in (result["error"] or "")


def test_invalid_query_type_returns_error() -> None:
    """Invalid query_type should return structured error."""
    result = self_telemetry_query_executor(query_type="unknown")
    assert result["success"] is False
    assert result["output"] == []
    assert "invalid query_type" in (result["error"] or "")


def test_output_is_capped_to_50_entries_with_truncation_marker() -> None:
    """Responses larger than 50 should be capped and marked truncated."""
    large_result = [{"idx": idx} for idx in range(60)]
    with patch(
        "personal_agent.tools.self_telemetry.query_events",
        return_value=large_result,
    ):
        result = self_telemetry_query_executor(query_type="events", limit=100)

    assert result["success"] is True
    assert len(result["output"]) == 50
    assert result["output"][48] == {"idx": 48}
    assert result["output"][49]["truncated"] is True
    assert result["output"][49]["total_available"] == 60


def _create_log_entry(
    event: str,
    timestamp: datetime,
    component: str = "test",
    trace_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create a synthetic telemetry log entry."""
    entry: dict[str, Any] = {
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


def _write_log_file(log_file: pathlib.Path, entries: list[dict[str, Any]]) -> None:
    """Write synthetic telemetry entries as JSONL."""
    with open(log_file, "w", encoding="utf-8") as file_handle:
        for entry in entries:
            file_handle.write(json.dumps(entry) + "\n")


@patch("personal_agent.telemetry.metrics._get_log_file_path")
def test_events_query_with_synthetic_jsonl_filters(
    mock_get_log_file: Any, tmp_path: pathlib.Path
) -> None:
    """Events query should filter by event, window, and component."""
    log_file = tmp_path / "current.jsonl"
    mock_get_log_file.return_value = log_file

    now = datetime.now(timezone.utc)
    entries = [
        _create_log_entry(
            event="model_call_completed",
            component="orchestrator",
            timestamp=now - timedelta(minutes=10),
        ),
        _create_log_entry(
            event="model_call_completed",
            component="llm_client",
            timestamp=now - timedelta(minutes=10),
        ),
        _create_log_entry(
            event="task_started",
            component="orchestrator",
            timestamp=now - timedelta(minutes=10),
        ),
        _create_log_entry(
            event="model_call_completed",
            component="orchestrator",
            timestamp=now - timedelta(hours=3),
        ),
    ]
    _write_log_file(log_file, entries)

    result = self_telemetry_query_executor(
        query_type="events",
        event="model_call_completed",
        window="1h",
        component="orchestrator",
    )

    assert result["success"] is True
    assert result["error"] is None
    assert len(result["output"]) == 1
    assert result["output"][0]["event"] == "model_call_completed"
    assert result["output"][0]["component"] == "orchestrator"


@patch("personal_agent.telemetry.metrics._get_log_file_path")
def test_trace_query_with_synthetic_jsonl_returns_chronological_entries(
    mock_get_log_file: Any, tmp_path: pathlib.Path
) -> None:
    """Trace query should return events sorted chronologically."""
    log_file = tmp_path / "current.jsonl"
    mock_get_log_file.return_value = log_file

    trace_id = "trace-chronological-123"
    now = datetime.now(timezone.utc)
    entries = [
        _create_log_entry(
            event="task_completed",
            trace_id=trace_id,
            timestamp=now - timedelta(seconds=5),
        ),
        _create_log_entry(
            event="request_received",
            trace_id=trace_id,
            timestamp=now - timedelta(seconds=20),
        ),
        _create_log_entry(
            event="task_started",
            trace_id=trace_id,
            timestamp=now - timedelta(seconds=10),
        ),
    ]
    _write_log_file(log_file, entries)

    result = self_telemetry_query_executor(query_type="trace", trace_id=trace_id)

    assert result["success"] is True
    timestamps = [entry["timestamp"] for entry in result["output"]]
    assert timestamps == sorted(timestamps)


@patch("personal_agent.telemetry.metrics._get_log_file_path")
def test_latency_query_with_synthetic_jsonl_returns_duration_breakdown(
    mock_get_log_file: Any, tmp_path: pathlib.Path
) -> None:
    """Latency query should include duration_ms in phase breakdown entries."""
    log_file = tmp_path / "current.jsonl"
    mock_get_log_file.return_value = log_file

    trace_id = "trace-latency-xyz"
    t0 = datetime.now(timezone.utc)
    entries = [
        _create_log_entry("request_received", trace_id=trace_id, timestamp=t0),
        _create_log_entry(
            "task_started",
            trace_id=trace_id,
            timestamp=t0 + timedelta(milliseconds=100),
        ),
        _create_log_entry(
            "state_transition",
            trace_id=trace_id,
            timestamp=t0 + timedelta(milliseconds=100),
            from_state="init",
        ),
        _create_log_entry(
            "state_transition",
            trace_id=trace_id,
            timestamp=t0 + timedelta(milliseconds=450),
            from_state="llm_call",
        ),
        _create_log_entry(
            "task_completed",
            trace_id=trace_id,
            timestamp=t0 + timedelta(milliseconds=1200),
        ),
        _create_log_entry(
            "reply_ready",
            trace_id=trace_id,
            timestamp=t0 + timedelta(milliseconds=1400),
        ),
    ]
    _write_log_file(log_file, entries)

    result = self_telemetry_query_executor(query_type="latency", trace_id=trace_id)

    assert result["success"] is True
    assert result["error"] is None
    assert result["output"]
    assert any("duration_ms" in phase for phase in result["output"])
