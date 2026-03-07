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

# Prefix-to-phase mapping for span name classification. Order matters:
# more specific prefixes (e.g. session_update, llm_call:router) before
# generic ones (session_, llm_call:).
_PHASE_MAP: list[tuple[str, str]] = [
    ("session_update", "synthesis"),
    ("session_", "setup"),
    ("orchestrator_setup", "setup"),
    ("context_window", "context"),
    ("memory_query", "context"),
    ("llm_call:router", "routing"),
    ("routing_", "routing"),
    ("llm_call:", "llm_inference"),
    ("tool_execution:", "tool_execution"),
    ("synthesis", "synthesis"),
    ("db_append_", "persistence"),
    ("memory_storage", "persistence"),
]


def _classify_phase(span_name: str) -> str:
    """Classify a span name into a phase category for aggregation.

    Args:
        span_name: The span name (e.g. "llm_call:router", "memory_query").

    Returns:
        Phase category: setup, context, routing, llm_inference, tool_execution,
        synthesis, persistence, or "other".
    """
    for prefix, phase in _PHASE_MAP:
        if span_name.startswith(prefix):
            return phase
    return "other"


@dataclass
class TimingSpan:
    """A single timed phase within a request lifecycle.

    Attributes:
        name: Phase name (e.g., "router_llm_call", "memory_query").
        sequence: Monotonically increasing step number within the request.
        phase: Phase category for aggregation (setup, context, routing, etc.).
        offset_ms: Milliseconds from request start when this span began.
        duration_ms: How long this span took in milliseconds.
        metadata: Arbitrary key-value pairs (model_role, tokens, etc.).
        parent_sequence: Reserved for nested span parent (unpopulated).
        span_id: Reserved for unique span ID (unpopulated).
        depth: Reserved for nesting depth (unpopulated).
    """

    name: str
    sequence: int
    phase: str
    offset_ms: float
    duration_ms: float
    metadata: dict[str, Any] = field(default_factory=dict)
    parent_sequence: int | None = None
    span_id: str = ""
    depth: int = 0


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
        self._sequence_counter: int = 0

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
        self._sequence_counter += 1
        end_ns = time.monotonic_ns()
        duration_ms = round((end_ns - start_ns) / 1_000_000, 2)
        offset_ms = round((start_ns - self._start_ns) / 1_000_000, 2)
        phase = _classify_phase(name)
        self._spans.append(
            TimingSpan(
                name=name,
                sequence=self._sequence_counter,
                phase=phase,
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
        self._sequence_counter += 1
        offset_ms = round((time.monotonic_ns() - self._start_ns) / 1_000_000, 2)
        phase = _classify_phase(name)
        self._spans.append(
            TimingSpan(
                name=name,
                sequence=self._sequence_counter,
                phase=phase,
                offset_ms=offset_ms,
                duration_ms=0.0,
                metadata=dict(metadata),
            )
        )

    def get_total_ms(self) -> float:
        """Total milliseconds elapsed since the timer was created."""
        return round((time.monotonic_ns() - self._start_ns) / 1_000_000, 2)

    def to_trace_summary(self) -> dict[str, Any]:
        """Aggregate duration and step counts per phase for trace summaries.

        Returns:
            Dict with total_duration_ms, total_steps, and phases_summary
            (per-phase duration_ms and steps).
        """
        phases: dict[str, dict[str, float | int]] = {}
        for span in self._spans:
            if span.phase not in phases:
                phases[span.phase] = {"duration_ms": 0.0, "steps": 0}
            phases[span.phase]["duration_ms"] += span.duration_ms
            phases[span.phase]["steps"] += 1
        return {
            "total_duration_ms": self.get_total_ms(),
            "total_steps": len(self._spans),
            "phases_summary": phases,
        }

    def to_breakdown(self) -> list[dict[str, Any]]:
        """Export all recorded spans as a list of dicts for ES indexing.

        Returns:
            List of span dicts sorted by offset_ms, plus a "total" entry.
            Each span dict has: name, sequence, phase (category), offset_ms,
            duration_ms, metadata.
        """
        result: list[dict[str, Any]] = []
        for span in sorted(self._spans, key=lambda s: s.offset_ms):
            entry: dict[str, Any] = {
                "name": span.name,
                "sequence": span.sequence,
                "phase": span.phase,
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
