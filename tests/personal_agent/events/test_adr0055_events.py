"""Tests for ADR-0055 event models: MetricsSampledEvent and ModeTransitionEvent."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from personal_agent.events.models import (
    CG_MODE_CONTROLLER,
    STREAM_METRICS_SAMPLED,
    STREAM_MODE_TRANSITION,
    MetricsSampledEvent,
    ModeTransitionEvent,
)
from personal_agent.governance.models import Mode


def test_metrics_sampled_event_requires_source_component() -> None:
    """MetricsSampledEvent must set source_component."""
    with pytest.raises(ValidationError):
        MetricsSampledEvent(
            sample_timestamp=datetime.now(UTC),
            metrics={"perf_system_cpu_load": 0.5},
            sample_interval_seconds=5.0,
        )  # type: ignore[call-arg]


def test_metrics_sampled_event_valid() -> None:
    event = MetricsSampledEvent(
        source_component="brainstem.sensors.metrics_daemon",
        sample_timestamp=datetime.now(UTC),
        metrics={"perf_system_cpu_load": 0.72, "perf_system_mem_used": 0.58},
        sample_interval_seconds=5.0,
    )
    assert event.event_type == "metrics.sampled"
    assert event.trace_id is None
    assert event.session_id is None
    assert event.schema_version == 1
    assert event.source_component == "brainstem.sensors.metrics_daemon"


def test_metrics_sampled_event_frozen() -> None:
    """EventBase uses frozen=True — immutable after creation."""
    event = MetricsSampledEvent(
        source_component="brainstem.sensors.metrics_daemon",
        sample_timestamp=datetime.now(UTC),
        metrics={},
        sample_interval_seconds=5.0,
    )
    with pytest.raises(ValidationError):
        event.metrics = {}  # type: ignore[misc]


def test_mode_transition_event_valid() -> None:
    event = ModeTransitionEvent(
        source_component="brainstem.mode_manager",
        from_mode=Mode.NORMAL,
        to_mode=Mode.ALERT,
        reason="cpu_high",
        transition_index=0,
    )
    assert event.event_type == "mode.transition"
    assert event.from_mode == Mode.NORMAL
    assert event.to_mode == Mode.ALERT
    assert event.sensor_snapshot == {}
    assert event.trace_id is None


def test_mode_transition_event_with_snapshot() -> None:
    event = ModeTransitionEvent(
        source_component="brainstem.mode_manager",
        from_mode=Mode.ALERT,
        to_mode=Mode.NORMAL,
        reason="cpu_recovered",
        sensor_snapshot={"perf_system_cpu_load": 0.3},
        transition_index=1,
    )
    assert event.sensor_snapshot["perf_system_cpu_load"] == 0.3


def test_stream_constants_exist() -> None:
    """Verify reserved stream name constants are exported."""
    assert STREAM_METRICS_SAMPLED == "stream:metrics.sampled"
    assert STREAM_MODE_TRANSITION == "stream:mode.transition"
    assert CG_MODE_CONTROLLER == "cg:mode-controller"
