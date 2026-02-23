"""Tests for RequestTimer inline span-based timing."""

import time

import pytest

from personal_agent.telemetry.request_timer import RequestTimer, TimingSpan


class TestRequestTimer:
    """Tests for RequestTimer."""

    def test_create_timer(self) -> None:
        timer = RequestTimer(trace_id="test-trace")
        assert timer.trace_id == "test-trace"
        assert timer.get_total_ms() >= 0

    def test_start_end_span(self) -> None:
        timer = RequestTimer(trace_id="test-trace")
        timer.start_span("test_phase")
        time.sleep(0.01)
        duration = timer.end_span("test_phase", key="value")

        assert duration > 0
        span = timer.get_span("test_phase")
        assert span is not None
        assert span.name == "test_phase"
        assert span.duration_ms > 0
        assert span.offset_ms >= 0
        assert span.metadata == {"key": "value"}

    def test_end_span_without_start(self) -> None:
        timer = RequestTimer(trace_id="test-trace")
        duration = timer.end_span("never_started")
        assert duration == 0.0

    def test_context_manager_span(self) -> None:
        timer = RequestTimer(trace_id="test-trace")
        with timer.span("block_phase", model="router"):
            time.sleep(0.01)

        span = timer.get_span("block_phase")
        assert span is not None
        assert span.duration_ms > 0
        assert span.metadata == {"model": "router"}

    def test_context_manager_records_on_exception(self) -> None:
        timer = RequestTimer(trace_id="test-trace")
        with pytest.raises(ValueError, match="boom"):
            with timer.span("failing_phase"):
                raise ValueError("boom")

        span = timer.get_span("failing_phase")
        assert span is not None
        assert span.duration_ms >= 0

    def test_multiple_spans(self) -> None:
        timer = RequestTimer(trace_id="test-trace")
        with timer.span("phase_a"):
            time.sleep(0.005)
        with timer.span("phase_b"):
            time.sleep(0.005)

        breakdown = timer.to_breakdown()
        phase_names = [s["phase"] for s in breakdown]
        assert "phase_a" in phase_names
        assert "phase_b" in phase_names
        assert "total" in phase_names

    def test_to_breakdown_sorted_by_offset(self) -> None:
        timer = RequestTimer(trace_id="test-trace")
        with timer.span("first"):
            time.sleep(0.005)
        with timer.span("second"):
            time.sleep(0.005)

        breakdown = timer.to_breakdown()
        non_total = [s for s in breakdown if s["phase"] != "total"]
        assert non_total[0]["phase"] == "first"
        assert non_total[1]["phase"] == "second"
        assert non_total[0]["offset_ms"] <= non_total[1]["offset_ms"]

    def test_to_breakdown_total_entry(self) -> None:
        timer = RequestTimer(trace_id="test-trace")
        with timer.span("work"):
            time.sleep(0.005)

        breakdown = timer.to_breakdown()
        total = next(s for s in breakdown if s["phase"] == "total")
        assert total["offset_ms"] == 0.0
        assert total["duration_ms"] > 0

    def test_record_instant(self) -> None:
        timer = RequestTimer(trace_id="test-trace")
        timer.record_instant("routing_decision", target="standard")

        span = timer.get_span("routing_decision")
        assert span is not None
        assert span.duration_ms == 0.0
        assert span.metadata == {"target": "standard"}

    def test_get_span_returns_last_match(self) -> None:
        timer = RequestTimer(trace_id="test-trace")
        with timer.span("repeated", iteration=1):
            pass
        with timer.span("repeated", iteration=2):
            pass

        span = timer.get_span("repeated")
        assert span is not None
        assert span.metadata["iteration"] == 2

    def test_get_span_returns_none_for_missing(self) -> None:
        timer = RequestTimer(trace_id="test-trace")
        assert timer.get_span("nonexistent") is None

    def test_repr(self) -> None:
        timer = RequestTimer(trace_id="abc-123")
        with timer.span("work"):
            pass
        r = repr(timer)
        assert "abc-123" in r
        assert "spans=1" in r

    def test_nested_spans(self) -> None:
        timer = RequestTimer(trace_id="test-trace")
        timer.start_span("outer")
        with timer.span("inner"):
            time.sleep(0.005)
        timer.end_span("outer")

        outer = timer.get_span("outer")
        inner = timer.get_span("inner")
        assert outer is not None
        assert inner is not None
        assert outer.duration_ms >= inner.duration_ms

    def test_metadata_in_breakdown(self) -> None:
        timer = RequestTimer(trace_id="test-trace")
        with timer.span("llm_call", model_role="router", tokens=500):
            pass

        breakdown = timer.to_breakdown()
        llm_phase = next(s for s in breakdown if s["phase"] == "llm_call")
        assert "metadata" in llm_phase
        assert llm_phase["metadata"]["model_role"] == "router"
        assert llm_phase["metadata"]["tokens"] == 500


class TestTimingSpan:
    """Tests for TimingSpan dataclass."""

    def test_create(self) -> None:
        span = TimingSpan(name="test", offset_ms=100.0, duration_ms=50.0)
        assert span.name == "test"
        assert span.offset_ms == 100.0
        assert span.duration_ms == 50.0
        assert span.metadata == {}

    def test_with_metadata(self) -> None:
        span = TimingSpan(
            name="llm",
            offset_ms=0,
            duration_ms=1000,
            metadata={"model": "router", "tokens": 250},
        )
        assert span.metadata["model"] == "router"
