"""Tests for ErrorPatternCluster, ErrorPatternDetectedEvent, and stream constants.

RED phase: these will fail until the types are added to events/models.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from personal_agent.events.models import (
    CG_ERROR_MONITOR,
    STREAM_ERRORS_PATTERN_DETECTED,
    ErrorPatternDetectedEvent,
    parse_stream_event,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_stream_constant_follows_domain_signal_convention() -> None:
    """Stream name must follow ADR-0054 <domain>.<signal> convention."""
    assert STREAM_ERRORS_PATTERN_DETECTED == "stream:errors.pattern_detected"


def test_cg_error_monitor_constant() -> None:
    """Consumer group name matches ADR-0056 §D2."""
    assert CG_ERROR_MONITOR == "cg:error-monitor"


# ---------------------------------------------------------------------------
# ErrorPatternDetectedEvent
# ---------------------------------------------------------------------------


def _make_event_payload() -> dict:
    return {
        "event_type": "errors.pattern_detected",
        "event_id": "test-event-id",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_component": "telemetry.error_monitor",
        "trace_id": None,
        "session_id": None,
        "schema_version": "1.0",
        "fingerprint": "abc123def456789a",
        "component": "tools.fetch_url",
        "event_name": "fetch_url_timeout",
        "error_type": "TimeoutError",
        "level": "ERROR",
        "occurrences": 12,
        "first_seen": datetime(2026, 4, 20, 0, 0, 0, tzinfo=timezone.utc).isoformat(),
        "last_seen": datetime(2026, 4, 24, 0, 0, 0, tzinfo=timezone.utc).isoformat(),
        "window_hours": 24,
        "sample_trace_ids": ["tid-1", "tid-2"],
        "sample_messages": ["Read timeout after 10s"],
    }


def test_error_pattern_detected_event_round_trips() -> None:
    """ErrorPatternDetectedEvent round-trips through model_validate."""
    payload = _make_event_payload()
    event = ErrorPatternDetectedEvent.model_validate(payload)
    assert event.event_type == "errors.pattern_detected"
    assert event.fingerprint == "abc123def456789a"
    assert event.component == "tools.fetch_url"
    assert event.occurrences == 12
    assert event.level == "ERROR"
    assert event.sample_trace_ids == ["tid-1", "tid-2"]
    assert event.trace_id is None
    assert event.source_component == "telemetry.error_monitor"


def test_error_pattern_detected_event_source_component_default() -> None:
    """source_component defaults to 'telemetry.error_monitor' per ADR-0056 D3."""
    payload = _make_event_payload()
    del payload["source_component"]
    event = ErrorPatternDetectedEvent.model_validate(payload)
    assert event.source_component == "telemetry.error_monitor"


def test_error_pattern_detected_event_sample_trace_ids_limited_to_five() -> None:
    """sample_trace_ids with more than 5 entries: validator trims to 5."""
    payload = _make_event_payload()
    payload["sample_trace_ids"] = [f"tid-{i}" for i in range(8)]
    event = ErrorPatternDetectedEvent.model_validate(payload)
    assert len(event.sample_trace_ids) <= 5


def test_error_pattern_detected_event_sample_messages_limited_to_three() -> None:
    """sample_messages with more than 3 entries: validator trims to 3."""
    payload = _make_event_payload()
    payload["sample_messages"] = ["msg1", "msg2", "msg3", "msg4", "msg5"]
    event = ErrorPatternDetectedEvent.model_validate(payload)
    assert len(event.sample_messages) <= 3


# ---------------------------------------------------------------------------
# parse_stream_event dispatch
# ---------------------------------------------------------------------------


def test_parse_stream_event_dispatches_error_pattern_detected() -> None:
    """parse_stream_event returns ErrorPatternDetectedEvent for the new event type."""
    payload = _make_event_payload()
    event = parse_stream_event(payload)
    assert isinstance(event, ErrorPatternDetectedEvent)
    assert event.fingerprint == "abc123def456789a"


def test_parse_stream_event_still_raises_on_unknown() -> None:
    """Existing unknown-type behavior is not broken."""
    with pytest.raises(ValueError, match="unknown event_type"):
        parse_stream_event({"event_type": "totally.unknown"})
