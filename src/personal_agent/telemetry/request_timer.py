"""Real-time request timing instrumentation.

Provides RequestTimer, a lightweight context object that records timing spans
inline as a request flows through the pipeline. Unlike the post-hoc log-scanning
approach in metrics.py, this captures precise monotonic-clock measurements
including phases that may not emit their own log events (e.g., memory graph
queries, DB lookups, context window processing).

Usage:
    timer = RequestTimer(trace_id="abc-123")

    with timer.span("session_lookup"):
        session = await repo.get(session_id)

    with timer.span("llm_call", model_role="router"):
        response = await client.respond(...)

    breakdown = timer.to_breakdown()
"""

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator


@dataclass
class TimingSpan:
    """A single timed phase within a request lifecycle.

    Attributes:
        name: Phase name (e.g., "router_llm_call", "memory_query").
        offset_ms: Milliseconds from request start when this span began.
        duration_ms: How long this span took in milliseconds.
        metadata: Arbitrary key-value pairs (model_role, tokens, etc.).
    """

    name: str
    offset_ms: float
    duration_ms: float
    metadata: dict[str, Any] = field(default_factory=dict)


class RequestTimer:
    """Records timing spans for a single request lifecycle.

    Uses monotonic clock for accurate duration measurement regardless of
    wall-clock adjustments. Spans can be nested and overlapping.

    Args:
        trace_id: Trace identifier for this request.
    """

    def __init__(self, trace_id: str) -> None:  # noqa: D107
        self.trace_id = trace_id
        self._start_ns: int = time.monotonic_ns()
        self._spans: list[TimingSpan] = []
        self._active: dict[str, int] = {}  # name -> start_ns

    def _elapsed_ms(self, from_ns: int | None = None) -> float:
        """Milliseconds elapsed since a given point (or request start)."""
        ref = from_ns if from_ns is not None else self._start_ns
        return (time.monotonic_ns() - ref) / 1_000_000

    def start_span(self, name: str) -> None:
        """Mark the beginning of a named span.

        Args:
            name: Unique span name. If a span with this name is already
                  active, the previous one is silently overwritten.
        """
        self._active[name] = time.monotonic_ns()

    def end_span(self, name: str, **metadata: Any) -> float:
        """Mark the end of a named span and record it.

        Args:
            name: Span name (must match a prior start_span call).
            **metadata: Additional key-value data to attach to the span.

        Returns:
            Duration of the span in milliseconds, or 0.0 if the span
            was never started.
        """
        start_ns = self._active.pop(name, None)
        if start_ns is None:
            return 0.0
        end_ns = time.monotonic_ns()
        duration_ms = round((end_ns - start_ns) / 1_000_000, 2)
        offset_ms = round((start_ns - self._start_ns) / 1_000_000, 2)
        self._spans.append(
            TimingSpan(
                name=name,
                offset_ms=offset_ms,
                duration_ms=duration_ms,
                metadata=dict(metadata),
            )
        )
        return duration_ms

    @contextmanager
    def span(self, name: str, **metadata: Any) -> Generator[None, None, None]:
        """Context manager that times a block of code as a named span.

        Args:
            name: Span name.
            **metadata: Additional data attached when the span closes.

        Yields:
            None. Timing is recorded on exit.
        """
        self.start_span(name)
        try:
            yield
        finally:
            self.end_span(name, **metadata)

    def record_instant(self, name: str, **metadata: Any) -> None:
        """Record a zero-duration marker at the current point in time.

        Useful for events like "routing_decision" that are instantaneous.

        Args:
            name: Event name.
            **metadata: Additional data.
        """
        offset_ms = round((time.monotonic_ns() - self._start_ns) / 1_000_000, 2)
        self._spans.append(
            TimingSpan(name=name, offset_ms=offset_ms, duration_ms=0.0, metadata=dict(metadata))
        )

    def get_total_ms(self) -> float:
        """Total milliseconds elapsed since the timer was created."""
        return round((time.monotonic_ns() - self._start_ns) / 1_000_000, 2)

    def to_breakdown(self) -> list[dict[str, Any]]:
        """Export all recorded spans as a list of dicts for ES indexing.

        Returns:
            List of span dicts sorted by offset_ms, plus a "total" entry.
            Each dict has: phase, offset_ms, duration_ms, metadata.
        """
        result: list[dict[str, Any]] = []
        for span in sorted(self._spans, key=lambda s: s.offset_ms):
            entry: dict[str, Any] = {
                "phase": span.name,
                "offset_ms": span.offset_ms,
                "duration_ms": span.duration_ms,
            }
            if span.metadata:
                entry["metadata"] = span.metadata
            result.append(entry)

        result.append(
            {
                "phase": "total",
                "offset_ms": 0.0,
                "duration_ms": self.get_total_ms(),
            }
        )
        return result

    def get_span(self, name: str) -> TimingSpan | None:
        """Look up a completed span by name (returns last match).

        Args:
            name: Span name.

        Returns:
            TimingSpan if found, None otherwise.
        """
        for span in reversed(self._spans):
            if span.name == name:
                return span
        return None

    def __repr__(self) -> str:  # noqa: D105
        completed = len(self._spans)
        active = len(self._active)
        total = self.get_total_ms()
        return f"RequestTimer(trace_id={self.trace_id!r}, spans={completed}, active={active}, total_ms={total})"
