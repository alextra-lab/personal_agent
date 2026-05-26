"""Trace context for request correlation and distributed tracing.

This module provides lightweight trace context propagation compatible with
OpenTelemetry concepts but without requiring the full OTel SDK.
"""

import uuid
from dataclasses import dataclass
from typing import Final
from uuid import UUID

SYSTEM_KIND_PREFIX: Final[str] = "system:"


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
        kind: Origin classification (ADR-0074 §3.6, FRE-376 Phase 4).
            ``"user"`` for traces initiated by an end user via the chat
            surface. ``"system:<source>"`` for traces minted by background
            paths that have no user-facing request — see
            :class:`SystemTraceContext`. Telemetry consumers can filter on
            this prefix to separate organic usage from scheduled work.
    """

    trace_id: str
    parent_span_id: str | None = None
    profile: str = "local"
    user_id: UUID | None = None
    session_id: str | None = None
    kind: str = "user"
    eval_mode: bool = False

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
            kind, and a new parent_span_id set to the generated span_id.
        """
        span_id = str(uuid.uuid4())
        return TraceContext(
            trace_id=self.trace_id,
            parent_span_id=span_id,
            profile=self.profile,
            user_id=self.user_id,
            session_id=self.session_id,
            kind=self.kind,
            eval_mode=self.eval_mode,
        ), span_id

    @property
    def is_system(self) -> bool:
        """Return True if this trace was minted by a non-user system path."""
        return self.kind.startswith(SYSTEM_KIND_PREFIX)


class SystemTraceContext:
    """Factory for non-user-driven :class:`TraceContext` instances.

    Per ADR-0074 §3.6 (FRE-376 Phase 4) ``TraceContext`` is non-optional on
    internal APIs. Functions that need to operate without a user-facing
    request — boot probes, scheduler ticks, periodic monitors, captain's
    log reflection, knowledge_api admin endpoints — mint their context
    through this factory so the resulting traces are clearly distinguishable
    from organic user traffic.

    The class is a namespace-only container: it has no state and no
    instances. All entry points are classmethods that return a plain
    :class:`TraceContext` with ``kind="system:<source>"`` set.
    """

    @staticmethod
    def new(
        source: str,
        *,
        profile: str = "local",
        session_id: str | None = None,
        user_id: UUID | None = None,
    ) -> TraceContext:
        """Mint a system-tagged :class:`TraceContext`.

        Args:
            source: Short identifier of the system-driven caller — for
                example ``"scheduler"``, ``"monitor"``, ``"reflection"``,
                ``"captains_log_feedback"``, ``"knowledge_api"``,
                ``"joinability_probe"``. Must be non-empty.
            profile: Execution profile name; defaults to ``"local"``.
            session_id: Optional session id when the system path operates
                on behalf of a known session (e.g. a scheduler tick that
                consolidates one session at a time).
            user_id: Optional user UUID when the system path operates on
                behalf of a known user.

        Returns:
            A new :class:`TraceContext` with a freshly generated
            ``trace_id`` and ``kind`` set to ``f"system:{source}"``.

        Raises:
            ValueError: If ``source`` is empty or contains whitespace.
        """
        if not source or source.strip() != source or " " in source:
            raise ValueError(
                f"SystemTraceContext source must be a non-empty, "
                f"whitespace-free identifier; got {source!r}"
            )
        return TraceContext(
            trace_id=str(uuid.uuid4()),
            profile=profile,
            user_id=user_id,
            session_id=session_id,
            kind=f"{SYSTEM_KIND_PREFIX}{source}",
        )
