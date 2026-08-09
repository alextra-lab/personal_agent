"""Trace context for request correlation, bridged to OpenTelemetry (ADR-0129 D1).

OpenTelemetry owns trace and span **identity**; this module owns everything else
about a request's identity. ``TraceContext`` *reads* its trace id from the active
OTel span rather than minting its own, and retains the five fields that are
load-bearing outside telemetry — ``user_id`` and ``authenticated`` (ADR-0064
per-user scoping, FRE-229/FRE-673 visibility filtering), ``session_id``
(session-scoped history isolation), ``eval_mode`` (FRE-375 substrate isolation)
and ``kind`` (organic vs scheduled work).

The bridge exists to end a divergence rather than to add a feature. FRE-1064's
structlog processor stamps every log record from the *active span*
(:func:`personal_agent.telemetry.logger._add_span_context`), so a context that
minted its own id alongside a live span would be guaranteed to disagree with its
own log records. Reading the span is what makes the two agree.

Trace ids render as 32 lowercase hex characters and span ids as 16 — the
OpenTelemetry rendering, matching what that processor writes to Elasticsearch and
what Tempo expects. Ids minted on the fallback path use the same shape, so a read
id and a minted id are indistinguishable and no second migration is needed when
FRE-1069 opens root spans on background entrypoints. The form stays coercible to
a Postgres ``UUID`` column, which is what ``trace_id`` is in eight-plus tables.

The OpenTelemetry API is a hard dependency of this package (``opentelemetry-sdk``
in ``pyproject.toml``), imported unconditionally at service startup — so no
import guard is needed here.
"""

import uuid
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from opentelemetry import trace as otel_trace

SYSTEM_KIND_PREFIX: Final[str] = "system:"

_TRACE_ID_HEX_WIDTH: Final[int] = 32
_SPAN_ID_HEX_WIDTH: Final[int] = 16


def _active_trace_id() -> str | None:
    """Return the active span's trace id, or None when there is no valid span.

    An OpenTelemetry context with no recording span reports an all-zero trace id
    that ``is_valid`` rejects. Never adopt it: rendered as hex it coerces to the
    nil UUID, which would collide on every row sharing it and violate
    ``captains_log_captures.trace_id NOT NULL UNIQUE``.

    Returns:
        The active span's trace id as 32 lowercase hex characters, or ``None``
        if no valid span is active.
    """
    span_context = otel_trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return format(span_context.trace_id, f"0{_TRACE_ID_HEX_WIDTH}x")


def read_or_mint_trace_id() -> str:
    """Read the active span's trace id, falling back to minting one.

    Public because request entrypoints need the id itself, not a whole
    :class:`TraceContext` — the ``/chat`` and ``/chat/stream`` handlers mint their
    trace id before any context exists (FRE-1215). An entrypoint that minted its
    own id instead would disagree with every log record of its own request, since
    :func:`personal_agent.telemetry.logger._add_span_context` stamps those from the
    active span (ADR-0129 D4); Postgres would then record one identifier and
    Elasticsearch another, and no cross-substrate join could match them.

    Returns:
        A 32-hex trace id — the active span's when one is active, otherwise a
        freshly minted random id in the identical shape.
    """
    return _active_trace_id() or uuid.uuid4().hex


@dataclass(frozen=True)
class TraceContext:
    """Lightweight trace context for request correlation.

    Provides minimal trace semantics compatible with OpenTelemetry:
    - trace_id: Unique identifier for an end-to-end user request or background task
    - parent_span_id: Optional parent span ID for nested operations

    This is a frozen dataclass and should never be modified after creation.
    Components should create new spans using new_span() rather than modifying
    the context.

    Formerly carried a ``profile`` field (ADR-0044 D5) for per-profile cost
    dashboards. Removed under ADR-0121 §8: a trace can span calls to several
    different models, so trace-wide profile was the wrong grain — provider
    and model are now stamped per call on ``model_call_completed`` instead
    (:mod:`personal_agent.llm_client.telemetry`).

    Attributes:
        trace_id: Identifier for the trace, as 32 lowercase hex characters.
            Read from the active OpenTelemetry span by :meth:`new_trace` and
            :meth:`SystemTraceContext.new` (ADR-0129 D1), or minted in the same
            shape when no span is active. Remains a plain field rather than a
            property so the nine direct constructions elsewhere in ``src/`` and
            the fifty-nine in ``tests/`` keep working — this is a bridge, not a
            flag-day change.
        parent_span_id: Optional parent span ID for nested operations.
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
    user_id: UUID | None = None
    session_id: str | None = None
    kind: str = "user"
    eval_mode: bool = False
    # FRE-673: whether the request carries a verified identity (CF Access). Propagated
    # to tool executors so memory-recall tools (search_memory) thread it into the
    # FRE-229 visibility filter and 'group'-visibility memory is revealed.
    authenticated: bool = False

    @classmethod
    def new_trace(
        cls,
        *,
        user_id: UUID | None = None,
        session_id: str | None = None,
        authenticated: bool = False,
    ) -> "TraceContext":
        """Start a new trace.

        The trace id is **read from the active OpenTelemetry span** (ADR-0129 D1)
        so this context and the log records emitted alongside it carry the same
        identity. With no span active — background paths until FRE-1069, and
        unit tests — a fresh id is minted in the same 32-hex shape.

        Args:
            user_id: Optional authenticated user UUID to propagate to child
                spans and tool executors.
            session_id: Optional session id to propagate.
            authenticated: Whether the request carries a verified identity (FRE-229
                / FRE-673); propagated to tool executors for visibility scoping.

        Returns:
            A new TraceContext carrying the active span's trace_id (or a minted
            one when no span is active) and no parent span.
        """
        return cls(
            trace_id=read_or_mint_trace_id(),
            user_id=user_id,
            session_id=session_id,
            authenticated=authenticated,
        )

    @property
    def span_id(self) -> str | None:
        """Return the active span's id, but only when it belongs to *this* trace.

        Read live rather than stored, so that inside nested spans it reports the
        span actually in effect. The same-trace guard is what makes that safe:
        ``trace_id`` is fixed when the context is built while this is resolved on
        access, so an unguarded read would happily pair ids from two *different*
        traces whenever a context outlives its span, is carried into an unrelated
        one, or was constructed directly. That would manufacture precisely the
        identity divergence this bridge exists to end, so the pair is instead
        either wholly consistent or absent.

        Returns:
            The active span's id as 16 lowercase hex characters, or ``None`` when
            no valid span is active or the active span belongs to another trace.
            ``None`` rather than a placeholder: ADR-0129 D8 drops sentinels.
        """
        span_context = otel_trace.get_current_span().get_span_context()
        if not span_context.is_valid:
            return None
        if format(span_context.trace_id, f"0{_TRACE_ID_HEX_WIDTH}x") != self.trace_id:
            return None
        return format(span_context.span_id, f"0{_SPAN_ID_HEX_WIDTH}x")

    def new_span(self) -> tuple["TraceContext", str]:
        """Create a child span within this trace.

        Returns:
            A tuple of (new TraceContext with this span as parent, new span_id).
            The new context has the same trace_id, user_id, session_id,
            kind, eval_mode, authenticated, and a new parent_span_id set to the
            generated span_id.
        """
        span_id = str(uuid.uuid4())
        return TraceContext(
            trace_id=self.trace_id,
            parent_span_id=span_id,
            user_id=self.user_id,
            session_id=self.session_id,
            kind=self.kind,
            eval_mode=self.eval_mode,
            authenticated=self.authenticated,
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

    **``kind`` — not the trace id — is what separates system from organic work**
    (ADR-0129 D1). Roughly twenty of these call sites sit inside served HTTP
    handlers, which run under a request root span; there the minted context
    adopts that span's trace id, so a system context and the turn it serves can
    legitimately share one. That is the correct outcome — it genuinely is one
    trace — and it is why filtering on ``kind`` / :attr:`TraceContext.is_system`
    is the supported way to tell them apart, never trace-id distinctness.
    """

    @staticmethod
    def new(
        source: str,
        *,
        session_id: str | None = None,
        user_id: UUID | None = None,
    ) -> TraceContext:
        """Mint a system-tagged :class:`TraceContext`.

        Args:
            source: Short identifier of the system-driven caller — for
                example ``"scheduler"``, ``"monitor"``, ``"reflection"``,
                ``"captains_log_feedback"``, ``"knowledge_api"``,
                ``"joinability_probe"``. Must be non-empty.
            session_id: Optional session id when the system path operates
                on behalf of a known session (e.g. a scheduler tick that
                consolidates one session at a time).
            user_id: Optional user UUID when the system path operates on
                behalf of a known user.

        Returns:
            A new :class:`TraceContext` carrying the active span's ``trace_id``
            (or a minted one when no span is active) and ``kind`` set to
            ``f"system:{source}"``.

        Raises:
            ValueError: If ``source`` is empty or contains whitespace.
        """
        if not source or source.strip() != source or " " in source:
            raise ValueError(
                f"SystemTraceContext source must be a non-empty, "
                f"whitespace-free identifier; got {source!r}"
            )
        return TraceContext(
            trace_id=read_or_mint_trace_id(),
            user_id=user_id,
            session_id=session_id,
            kind=f"{SYSTEM_KIND_PREFIX}{source}",
        )
