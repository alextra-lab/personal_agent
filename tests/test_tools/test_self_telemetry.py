"""Tests for self_telemetry_query tool."""

import json
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.telemetry.events import (
    ERROR_TREND_DECREASING,
    ERROR_TREND_INCREASING,
    ERROR_TREND_STABLE,
)
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
            to_state="init",
        ),
        _create_log_entry(
            "state_transition",
            trace_id=trace_id,
            timestamp=t0 + timedelta(milliseconds=450),
            from_state="init",
            to_state="llm_call",
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


# =============================================================================
# Tests for new query types: health, errors, interactions, performance
# =============================================================================


@patch("personal_agent.tools.self_telemetry._load_captures")
@patch("personal_agent.tools.self_telemetry._assess_component_health")
@patch("personal_agent.tools.self_telemetry._get_system_status")
@patch("personal_agent.tools.self_telemetry._determine_health_status")
@patch("personal_agent.tools.self_telemetry._generate_alerts")
def test_health_query_dispatches_correctly(
    mock_generate_alerts: Any,
    mock_determine_status: Any,
    mock_get_system: Any,
    mock_assess_health: Any,
    mock_load_captures: Any,
) -> None:
    """Health query should call all helper functions and return structured report."""
    # Mock captures
    mock_captures = [
        TaskCapture(
            trace_id="trace-1",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="completed",
            duration_ms=1000,
        ),
        TaskCapture(
            trace_id="trace-2",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="completed",
            duration_ms=2000,
        ),
    ]
    mock_load_captures.return_value = mock_captures
    mock_assess_health.return_value = {
        "llm": {"status": "healthy", "calls": 10, "errors": 0},
        "tools": {"status": "healthy", "calls": 5, "errors": 0},
    }
    mock_get_system.return_value = {"mode": "NORMAL", "cpu_avg": 15.0, "mem_avg": 50.0}
    mock_determine_status.return_value = "healthy"
    mock_generate_alerts.return_value = []

    result = self_telemetry_query_executor(query_type="health", window="1h")

    assert result["success"] is True
    assert result["error"] is None
    assert result["output"]["status"] == "healthy"
    assert result["output"]["interactions"]["total"] == 2
    assert result["output"]["interactions"]["success_rate"] == 1.0
    mock_load_captures.assert_called_once_with(window="1h", last_n=None)


@patch("personal_agent.tools.self_telemetry._load_captures")
def test_health_query_with_last_n(
    mock_load_captures: Any,
) -> None:
    """Health query with last_n should override window parameter."""
    mock_load_captures.return_value = []

    result = self_telemetry_query_executor(query_type="health", last_n=5)

    assert result["success"] is True
    mock_load_captures.assert_called_once_with(window=None, last_n=5)


@patch("personal_agent.tools.self_telemetry._load_captures")
@patch("personal_agent.tools.self_telemetry.query_events")
@patch("personal_agent.tools.self_telemetry._compute_error_trend")
def test_errors_query_dispatches_correctly(
    mock_compute_trend: Any,
    mock_query_events: Any,
    mock_load_captures: Any,
) -> None:
    """Errors query should call helper functions and return grouped analysis."""
    # Mock captures with failures
    mock_captures = [
        TaskCapture(
            trace_id="trace-1",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="failed",
            duration_ms=1000,
        ),
    ]
    mock_load_captures.return_value = mock_captures
    mock_query_events.return_value = []
    mock_compute_trend.return_value = "stable"

    result = self_telemetry_query_executor(query_type="errors", window="24h")

    assert result["success"] is True
    assert result["error"] is None
    assert "by_type" in result["output"]
    assert "by_component" in result["output"]
    assert "trend" in result["output"]
    assert result["output"]["trend"] == "stable"


@patch("personal_agent.tools.self_telemetry._load_captures")
def test_interactions_query_dispatches_correctly(
    mock_load_captures: Any,
) -> None:
    """Interactions query should return interaction list with summary."""
    mock_captures = [
        TaskCapture(
            trace_id="trace-1",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="Hello world",
            outcome="completed",
            duration_ms=1000,
            tools_used=["mcp_perplexity_ask"],
            steps=[{"type": "planning"}],
        ),
        TaskCapture(
            trace_id="trace-2",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="Test",
            outcome="completed",
            duration_ms=2000,
            tools_used=["mcp_perplexity_ask", "search_memory"],
            steps=[{"type": "planning"}, {"type": "tool_execution"}],
        ),
    ]
    mock_load_captures.return_value = mock_captures

    result = self_telemetry_query_executor(query_type="interactions", last_n=10)

    assert result["success"] is True
    assert result["error"] is None
    assert result["output"]["count"] == 2
    assert len(result["output"]["interactions"]) == 2
    assert result["output"]["summary"]["success_rate"] == 1.0
    assert result["output"]["interactions"][0]["user_message_preview"] == "Hello world"
    assert result["output"]["interactions"][1]["user_message_preview"] == "Test"


@patch("personal_agent.tools.self_telemetry._load_captures")
def test_performance_query_dispatches_correctly(
    mock_load_captures: Any,
) -> None:
    """Performance query should return latency/throughput metrics."""
    mock_captures = [
        TaskCapture(
            trace_id="trace-1",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="completed",
            duration_ms=1000,
            tools_used=["mcp_perplexity_ask"],
        ),
        TaskCapture(
            trace_id="trace-2",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="completed",
            duration_ms=2000,
            tools_used=["mcp_perplexity_ask"],
        ),
        TaskCapture(
            trace_id="trace-3",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="failed",
            duration_ms=5000,
            tools_used=["search_memory"],
        ),
    ]
    mock_load_captures.return_value = mock_captures

    result = self_telemetry_query_executor(query_type="performance", window="24h")

    assert result["success"] is True
    assert result["error"] is None
    assert "throughput" in result["output"]
    assert "latency" in result["output"]
    assert "by_outcome" in result["output"]
    assert "top_tools" in result["output"]
    assert result["output"]["throughput"]["total_interactions"] == 3


def test_new_query_types_in_tool_definition() -> None:
    """Tool definition should list health, errors, interactions, performance query types."""
    description = self_telemetry_query_tool.description

    assert "health" in description
    assert "errors" in description
    assert "interactions" in description
    assert "performance" in description

    # Check query_type parameter includes new types
    parameters = {p.name: p for p in self_telemetry_query_tool.parameters}
    assert "query_type" in parameters
    assert "health" in parameters["query_type"].description
    assert "errors" in parameters["query_type"].description


@patch("personal_agent.tools.self_telemetry._load_captures")
def test_graceful_handling_empty_captures(
    mock_load_captures: Any,
) -> None:
    """Query types should handle empty captures gracefully."""
    mock_load_captures.return_value = []

    # Health query with no captures should return valid structure with defaults
    result = self_telemetry_query_executor(query_type="health", window="1h")

    assert result["success"] is True
    assert result["output"]["interactions"]["total"] == 0
    assert result["output"]["interactions"]["success_rate"] == 1.0  # Default when no data


def test_compute_error_trend_with_named_window_returns_valid_trend() -> None:
    """_compute_error_trend should handle named windows and return a valid trend constant."""
    from personal_agent.tools.self_telemetry import _compute_error_trend

    # Test with "today" named window - should return one of the ERROR_TREND_* constants
    trend = _compute_error_trend("today")

    assert trend in (ERROR_TREND_INCREASING, ERROR_TREND_STABLE, ERROR_TREND_DECREASING)


@patch("personal_agent.tools.self_telemetry.query_events")
def test_compute_error_trend_with_named_window_uses_24h_previous(
    mock_query_events: Any,
) -> None:
    """Named window trend comparison should use 24h as preceding window."""
    from personal_agent.tools.self_telemetry import _compute_error_trend

    # Mock error counts: today has 5 errors, yesterday has 2
    def query_side_effect(event: str, window_str: str) -> list[dict[str, Any]]:
        if window_str == "today":
            return [{"event": event, "timestamp": "2026-03-15T10:00:00Z"}] * 5
        elif window_str == "24h":
            return [{"event": event, "timestamp": "2026-03-14T10:00:00Z"}] * 2
        return []

    mock_query_events.side_effect = query_side_effect

    trend = _compute_error_trend("today")

    # 5 errors today vs 2 yesterday = 2.5x increase, should be "increasing"
    assert trend == ERROR_TREND_INCREASING


@patch("personal_agent.tools.self_telemetry.query_events")
def test_compute_error_trend_named_window_stable(mock_query_events: Any) -> None:
    """Named window with similar error counts should return stable trend."""
    from personal_agent.tools.self_telemetry import _compute_error_trend

    # Mock equal error counts
    mock_query_events.return_value = [{"event": "model_call_error"}] * 3

    trend = _compute_error_trend("yesterday")

    assert trend == ERROR_TREND_STABLE


# =============================================================================
# Tests for compute_trend function (FRE-109)
# =============================================================================


def test_compute_trend_increasing() -> None:
    """compute_trend should return increasing when current > 1.5x previous."""
    from personal_agent.tools.self_telemetry import compute_trend

    assert compute_trend(10, 5) == ERROR_TREND_INCREASING  # 2x
    assert compute_trend(8, 5) == ERROR_TREND_INCREASING  # 1.6x
    assert compute_trend(100, 10) == ERROR_TREND_INCREASING  # 10x


def test_compute_trend_decreasing() -> None:
    """compute_trend should return decreasing when current < 0.5x previous."""
    from personal_agent.tools.self_telemetry import compute_trend

    assert compute_trend(2, 5) == ERROR_TREND_DECREASING  # 0.4x
    assert compute_trend(1, 10) == ERROR_TREND_DECREASING  # 0.1x


def test_compute_trend_stable() -> None:
    """compute_trend should return stable for intermediate values."""
    from personal_agent.tools.self_telemetry import compute_trend

    assert compute_trend(5, 5) == ERROR_TREND_STABLE  # 1x
    assert compute_trend(7, 5) == ERROR_TREND_STABLE  # 1.4x
    assert compute_trend(3, 5) == ERROR_TREND_STABLE  # 0.6x
    assert compute_trend(10, 12) == ERROR_TREND_STABLE  # ~0.83x


def test_compute_trend_edge_case_zero_previous() -> None:
    """compute_trend should return increasing if current > 0 and previous = 0."""
    from personal_agent.tools.self_telemetry import compute_trend

    assert compute_trend(0, 0) == ERROR_TREND_STABLE  # both zero
    assert compute_trend(5, 0) == ERROR_TREND_INCREASING  # previous zero, current > 0


# =============================================================================
# Tests for latency percentiles (FRE-109)
# =============================================================================


def test_compute_latency_stats_with_percentiles() -> None:
    """_compute_latency_stats should include p50, p75, p90, p95."""
    from personal_agent.tools.self_telemetry import _compute_latency_stats

    durations = list(range(1, 101))  # 1 to 100 ms

    stats = _compute_latency_stats(durations)

    assert stats["avg_ms"] == 50.5  # (1+100)/2
    assert stats["min_ms"] == 1
    assert stats["max_ms"] == 100
    # For 100 samples: idx 50->51, idx 75->76, idx 90->91, idx 95->96
    assert stats["p50_ms"] == 51
    assert stats["p75_ms"] == 76
    assert stats["p90_ms"] == 91
    assert stats["p95_ms"] == 96


def test_compute_latency_stats_few_samples_no_percentiles() -> None:
    """_compute_latency_stats should return None for percentiles with < 4 samples."""
    from personal_agent.tools.self_telemetry import _compute_latency_stats

    durations = [10, 20, 30]  # Only 3 samples — below threshold of 4

    stats = _compute_latency_stats(durations)

    assert stats["avg_ms"] == 20.0
    assert stats["min_ms"] == 10
    assert stats["max_ms"] == 30
    # All percentiles should be None with < 4 samples
    assert stats["p50_ms"] is None
    assert stats["p75_ms"] is None
    assert stats["p90_ms"] is None
    assert stats["p95_ms"] is None


def test_compute_latency_stats_empty() -> None:
    """_compute_latency_stats should return None for all values with empty input."""
    from personal_agent.tools.self_telemetry import _compute_latency_stats

    stats = _compute_latency_stats([])

    assert stats["avg_ms"] is None
    assert stats["min_ms"] is None
    assert stats["max_ms"] is None
    assert stats["p50_ms"] is None
    assert stats["p75_ms"] is None
    assert stats["p90_ms"] is None
    assert stats["p95_ms"] is None


# =============================================================================
# Tests for performance query with trend detection (FRE-109)
# =============================================================================


@patch("personal_agent.tools.self_telemetry._load_captures")
def test_performance_query_includes_latency_trend(mock_load_captures: Any) -> None:
    """Performance query should include latency_trend in output."""
    from personal_agent.telemetry.events import ERROR_TREND_STABLE

    mock_captures = [
        TaskCapture(
            trace_id="trace-1",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="completed",
            duration_ms=1000,
            tools_used=[],
        ),
    ]
    mock_load_captures.return_value = mock_captures

    result = self_telemetry_query_executor(query_type="performance", window="24h")

    assert result["success"] is True
    assert "latency_trend" in result["output"]
    assert result["output"]["latency_trend"] in (
        ERROR_TREND_INCREASING,
        ERROR_TREND_STABLE,
        ERROR_TREND_DECREASING,
    )


# =============================================================================
# E2E validation tests for FRE-107 repro matrix (FRE-110)
# =============================================================================


@patch("personal_agent.tools.self_telemetry._load_captures")
def test_repro_matrix_how_are_you_doing(mock_load_captures: Any) -> None:
    """'How are you doing?' should produce health query with well-structured response."""
    mock_captures = [
        TaskCapture(
            trace_id="trace-1",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="completed",
            duration_ms=1000,
            tools_used=[],
        ),
        TaskCapture(
            trace_id="trace-2",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="completed",
            duration_ms=2000,
            tools_used=[],
        ),
    ]
    mock_load_captures.return_value = mock_captures

    result = self_telemetry_query_executor(query_type="health", window="1h")

    assert result["success"] is True
    assert "output" in result
    assert "status" in result["output"]
    assert "interactions" in result["output"]
    assert "components" in result["output"]
    # Response should be structured, not raw events
    assert isinstance(result["output"]["status"], str)
    assert result["output"]["status"] in ("healthy", "degraded", "unhealthy")


@patch("personal_agent.tools.self_telemetry._load_captures")
@patch("personal_agent.tools.self_telemetry.query_events")
def test_repro_matrix_any_errors_recently(mock_query_events: Any, mock_load_captures: Any) -> None:
    """'Any errors recently?' should produce errors query with grouped analysis."""
    mock_query_events.return_value = [
        {"event": "model_call_error", "timestamp": "2026-03-15T10:00:00Z"},
    ]
    mock_captures: list[TaskCapture] = []
    mock_load_captures.return_value = mock_captures

    result = self_telemetry_query_executor(query_type="errors", window="today")

    assert result["success"] is True
    assert "output" in result
    assert "by_type" in result["output"]
    assert "by_component" in result["output"]
    assert "recent" in result["output"]
    assert "trend" in result["output"]
    # Response should be structured, not raw events
    assert isinstance(result["output"]["by_type"], dict)


@patch("personal_agent.tools.self_telemetry._load_captures")
def test_repro_matrix_errors_last_5_interactions(mock_load_captures: Any) -> None:
    """'Errors in the last 5 interactions' should use last_n scoping."""
    mock_captures = [
        TaskCapture(
            trace_id="trace-1",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="completed",
            duration_ms=1000,
            tools_used=[],
        ),
        TaskCapture(
            trace_id="trace-2",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="failed",
            duration_ms=2000,
            tools_used=[],
        ),
    ]
    mock_load_captures.return_value = mock_captures

    result = self_telemetry_query_executor(query_type="errors", last_n=5)

    assert result["success"] is True
    # Should have been called with last_n=5
    mock_load_captures.assert_called_once_with(window=None, last_n=5)


@patch("personal_agent.tools.self_telemetry._load_captures")
def test_repro_matrix_is_agent_healthy(mock_load_captures: Any) -> None:
    """'Is the agent healthy?' should produce health query."""
    mock_captures = [
        TaskCapture(
            trace_id="trace-1",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="completed",
            duration_ms=1000,
            tools_used=[],
        ),
    ]
    mock_load_captures.return_value = mock_captures

    result = self_telemetry_query_executor(query_type="health")

    assert result["success"] is True
    assert "status" in result["output"]
    assert "window" in result["output"]
    # Verify it returns a status verdict, not raw data
    assert result["output"]["status"] in ("healthy", "degraded", "unhealthy")


@patch("personal_agent.tools.self_telemetry._load_captures")
@patch("personal_agent.tools.self_telemetry.get_request_latency_breakdown")
def test_repro_matrix_why_was_that_slow(
    mock_latency_breakdown: Any, mock_load_captures: Any
) -> None:
    """'Why was that slow?' should use latency query with trace_id."""
    mock_latency_breakdown.return_value = [
        {"phase": "llm_call", "duration_ms": 1000},
        {"phase": "tool_execution", "duration_ms": 500},
    ]
    mock_load_captures.return_value = []

    result = self_telemetry_query_executor(query_type="latency", trace_id="current")

    assert result["success"] is True
    assert "output" in result
    # Should return phase breakdown with durations
    assert len(result["output"]) > 0
    assert any("duration_ms" in phase for phase in result["output"])


@patch("personal_agent.tools.self_telemetry._load_captures")
def test_repro_matrix_what_have_you_been_working_on(mock_load_captures: Any) -> None:
    """'What have you been working on?' should produce interactions query."""
    mock_captures = [
        TaskCapture(
            trace_id="trace-1",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="Hello",
            outcome="completed",
            duration_ms=1000,
            tools_used=["mcp_perplexity_ask"],
            steps=[{"type": "planning"}],
        ),
        TaskCapture(
            trace_id="trace-2",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="World",
            outcome="completed",
            duration_ms=2000,
            tools_used=["search_memory"],
            steps=[{"type": "planning"}, {"type": "tool_execution"}],
        ),
    ]
    mock_load_captures.return_value = mock_captures

    result = self_telemetry_query_executor(query_type="interactions", last_n=5)

    assert result["success"] is True
    assert "interactions" in result["output"]
    assert "count" in result["output"]
    assert result["output"]["count"] == 2
    # Response should be structured, not raw events
    assert len(result["output"]["interactions"]) == 2


@patch("personal_agent.tools.self_telemetry._load_captures")
def test_repro_matrix_am_i_getting_slower(mock_load_captures: Any) -> None:
    """'Am I getting slower?' should produce performance query with trend."""
    mock_captures = [
        TaskCapture(
            trace_id="trace-1",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="completed",
            duration_ms=1000,
            tools_used=[],
        ),
        TaskCapture(
            trace_id="trace-2",
            session_id="sess-1",
            timestamp=datetime.now(timezone.utc),
            user_message="test",
            outcome="completed",
            duration_ms=2000,
            tools_used=[],
        ),
    ]
    mock_load_captures.return_value = mock_captures

    result = self_telemetry_query_executor(query_type="performance", window="today")

    assert result["success"] is True
    assert "latency" in result["output"]
    assert "throughput" in result["output"]
    assert "latency_trend" in result["output"]
    # Response should be structured, not raw events
    assert "avg_ms" in result["output"]["latency"]
    assert result["output"]["latency_trend"] in (
        ERROR_TREND_INCREASING,
        ERROR_TREND_STABLE,
        ERROR_TREND_DECREASING,
    )


@patch("personal_agent.tools.self_telemetry._load_captures")
@patch("personal_agent.tools.self_telemetry.query_events")
def test_repro_matrix_show_failures_this_week(
    mock_query_events: Any, mock_load_captures: Any
) -> None:
    """'Show me failures this week' should use errors query with named window."""
    mock_query_events.return_value = [
        {"event": "task_failed", "timestamp": "2026-03-15T10:00:00Z"},
    ]
    mock_captures: list[TaskCapture] = []
    mock_load_captures.return_value = mock_captures

    result = self_telemetry_query_executor(query_type="errors", window="this_week")

    assert result["success"] is True
    assert "by_type" in result["output"]
    assert "by_component" in result["output"]
    assert "trend" in result["output"]
    # Verify named window handling
    assert result["output"]["scope"] == "this_week"
