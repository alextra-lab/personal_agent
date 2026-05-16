"""Trace context for request correlation and distributed tracing.

This module provides lightweight trace context propagation compatible with
OpenTelemetry concepts but without requiring the full OTel SDK.
"""

import uuid
from dataclasses import dataclass
from uuid import UUID


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
        user_id: Owning user UUID propagated from the authenticated request
            (ADR-0064). Tool executors that receive ``ctx`` read this for
            per-user scoping (e.g. notes_search, recall_personal_history).
            None for background / unauthenticated paths.
        session_id: Originating session id, when applicable. Same propagation
            rules as ``user_id``; tools may pass it through to row-level FKs.
    """

    trace_id: str
    parent_span_id: str | None = None
    profile: str = "local"
    user_id: UUID | None = None
    session_id: str | None = None

    @classmethod
    def new_trace(
        cls,
        profile: str = "local",
        *,
        user_id: UUID | None = None,
        session_id: str | None = None,
    ) -> "TraceContext":
        """Start a new trace.

        Args:
            profile: Execution profile name for this trace (default: "local").
            user_id: Optional authenticated user UUID to propagate to child
                spans and tool executors.
            session_id: Optional session id to propagate.

        Returns:
            A new TraceContext with a generated trace_id and no parent span.
        """
        return cls(
            trace_id=str(uuid.uuid4()),
            profile=profile,
            user_id=user_id,
            session_id=session_id,
        )

    def new_span(self) -> tuple["TraceContext", str]:
        """Create a child span within this trace.

        Returns:
            A tuple of (new TraceContext with this span as parent, new span_id).
            The new context has the same trace_id, profile, user_id, session_id,
            and a new parent_span_id set to the generated span_id.
        """
        span_id = str(uuid.uuid4())
        return TraceContext(
            trace_id=self.trace_id,
            parent_span_id=span_id,
            profile=self.profile,
            user_id=self.user_id,
            session_id=self.session_id,
        ), span_id
