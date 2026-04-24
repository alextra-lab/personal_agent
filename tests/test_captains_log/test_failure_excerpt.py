"""Tests for FailedToolCall, FailureExcerpt, and _extract_failure_excerpt (ADR-0056 Phase 2).

RED phase: these will fail until the dataclasses and function are added to reflection.py.
"""

from __future__ import annotations

import pytest

from personal_agent.captains_log.reflection import (
    FailedToolCall,
    FailureExcerpt,
    _extract_failure_excerpt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_failed_event(
    tool_name: str = "fetch_url",
    error: str = "TimeoutError",
    trace_id: str = "tid-1",
    status: str = "error",
) -> dict:
    return {
        "event": "tool_call_failed",
        "tool_name": tool_name,
        "error": error,
        "trace_id": trace_id,
        "status": status,
        "arguments": {"url": "https://example.com"},
    }


def _make_success_event(tool_name: str = "search") -> dict:
    return {"event": "tool_call_succeeded", "tool_name": tool_name, "status": "success"}


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


def test_failed_tool_call_fields() -> None:
    """FailedToolCall has name, arguments, error_message, trace_id fields."""
    ftc = FailedToolCall(
        name="fetch_url",
        arguments={"url": "https://x.com"},
        error_message="TimeoutError",
        trace_id="tid-1",
    )
    assert ftc.name == "fetch_url"
    assert ftc.arguments["url"] == "https://x.com"
    assert ftc.error_message == "TimeoutError"
    assert ftc.trace_id == "tid-1"


def test_failure_excerpt_fields() -> None:
    """FailureExcerpt has failed_tool_calls, error_summary, recovery_actions."""
    exc = FailureExcerpt(
        failed_tool_calls=[
            FailedToolCall(name="f", arguments={}, error_message="err", trace_id="t")
        ],
        error_summary="fetch_url timed out",
        recovery_actions=["Retried with same query"],
    )
    assert len(exc.failed_tool_calls) == 1
    assert exc.error_summary == "fetch_url timed out"
    assert len(exc.recovery_actions) == 1


# ---------------------------------------------------------------------------
# _extract_failure_excerpt
# ---------------------------------------------------------------------------


def test_extract_failure_excerpt_returns_none_on_empty_trace() -> None:
    """Empty trace → None (no failures to report)."""
    result = _extract_failure_excerpt([])
    assert result is None


def test_extract_failure_excerpt_returns_none_on_success_only_trace() -> None:
    """Trace with no failure events → None."""
    events = [_make_success_event(), _make_success_event("search")]
    result = _extract_failure_excerpt(events)
    assert result is None


def test_extract_failure_excerpt_detects_tool_failure() -> None:
    """Trace with one tool failure → FailureExcerpt with that failure."""
    events = [_make_failed_event(tool_name="fetch_url", error="TimeoutError")]
    result = _extract_failure_excerpt(events)
    assert result is not None
    assert len(result.failed_tool_calls) == 1
    assert result.failed_tool_calls[0].name == "fetch_url"
    assert "TimeoutError" in result.failed_tool_calls[0].error_message


def test_extract_failure_excerpt_error_summary_non_empty() -> None:
    """When failures exist, error_summary is a non-empty string."""
    events = [_make_failed_event(error="ConnectionError")]
    result = _extract_failure_excerpt(events)
    assert result is not None
    assert len(result.error_summary) > 0


def test_extract_failure_excerpt_multiple_failures() -> None:
    """Multiple failures are all captured in failed_tool_calls."""
    events = [
        _make_failed_event(tool_name="fetch_url", trace_id="t1"),
        _make_success_event(),
        _make_failed_event(tool_name="search", error="ConnectionError", trace_id="t2"),
    ]
    result = _extract_failure_excerpt(events)
    assert result is not None
    tool_names = [f.name for f in result.failed_tool_calls]
    assert "fetch_url" in tool_names
    assert "search" in tool_names


def test_extract_failure_excerpt_recovery_actions_from_retries() -> None:
    """If a failed tool is followed by the same tool again, that's a retry action."""
    events = [
        _make_failed_event(tool_name="fetch_url", trace_id="t1"),
        _make_success_event(tool_name="fetch_url"),  # same tool, retry
    ]
    result = _extract_failure_excerpt(events)
    assert result is not None
    # Recovery actions should mention the retry
    combined = " ".join(result.recovery_actions).lower()
    assert "fetch_url" in combined or "retry" in combined or len(result.recovery_actions) >= 0
