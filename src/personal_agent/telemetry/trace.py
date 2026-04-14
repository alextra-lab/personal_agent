"""Trace context for request correlation and distributed tracing.

This module provides lightweight trace context propagation compatible with
OpenTelemetry concepts but without requiring the full OTel SDK.
"""

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class TraceContext:
    """Lightweight trace context for request correlation.

    Provides minimal trace semantics compatible with OpenTelemetry:
    - trace_id: Unique identifier for an end-to-end user request or background task
    - parent_span_id: Optional parent span ID for nested operations

    This is a frozen dataclass and should never be modified after creation.
    Components should create new spans using new_span() rather than modifying
    the context.

    Attributes:
        trace_id: Unique identifier for the trace (UUID string).
        parent_span_id: Optional parent span ID for nested operations.
        profile: Execution profile name bound to this trace (ADR-0044 D5).
            Defaults to "local". All telemetry emitted within this trace is
            tagged with this value to enable per-profile cost dashboards and
            A/B comparisons.
    """

    trace_id: str
    parent_span_id: str | None = None
    profile: str = "local"

    @classmethod
    def new_trace(cls, profile: str = "local") -> "TraceContext":
        """Start a new trace.

        Args:
            profile: Execution profile name for this trace (default: "local").

        Returns:
            A new TraceContext with a generated trace_id and no parent span.
        """
        return cls(trace_id=str(uuid.uuid4()), profile=profile)

    def new_span(self) -> tuple["TraceContext", str]:
        """Create a child span within this trace.

        Returns:
            A tuple of (new TraceContext with this span as parent, new span_id).
            The new context has the same trace_id, profile, and a new
            parent_span_id set to the generated span_id.
        """
        span_id = str(uuid.uuid4())
        return TraceContext(
            trace_id=self.trace_id, parent_span_id=span_id, profile=self.profile
        ), span_id
