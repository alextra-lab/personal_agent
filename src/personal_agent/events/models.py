"""Event models for the Redis Streams event bus (ADR-0041).

All event models are frozen Pydantic models with a ``event_type`` literal
discriminator — consistent with the project's discriminated-union coding
standard.  Events carry identifiers and metadata, not large payloads;
consumers fetch full data from the source system when needed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from personal_agent.governance.models import Mode

# ---------------------------------------------------------------------------
# Stream and consumer-group constants
# ---------------------------------------------------------------------------

STREAM_REQUEST_CAPTURED = "stream:request.captured"
"""Stream for request-captured events (Phase 1)."""

STREAM_REQUEST_COMPLETED = "stream:request.completed"
"""Stream for request-completed events (Phase 2 — ES + session writer)."""

STREAM_CONSOLIDATION_COMPLETED = "stream:consolidation.completed"
"""Stream for consolidation-completed events (Phase 3)."""

STREAM_PROMOTION_ISSUE_CREATED = "stream:promotion.issue_created"
"""Stream for promotion-issue-created events (Phase 3)."""

STREAM_FEEDBACK_RECEIVED = "stream:feedback.received"
"""Stream for feedback-received events (Phase 3)."""

STREAM_SYSTEM_IDLE = "stream:system.idle"
"""Stream for system-idle events (Phase 3)."""

STREAM_MEMORY_ACCESSED = "stream:memory.accessed"
"""Stream for memory-accessed events (Phase 4)."""

STREAM_MEMORY_ENTITIES_UPDATED = "stream:memory.entities_updated"
"""Stream for memory-entities-updated events (Phase 4)."""

CG_CONSOLIDATOR = "cg:consolidator"
"""Consumer group: brainstem consolidator."""

CG_ES_INDEXER = "cg:es-indexer"
"""Consumer group: request trace indexing to Elasticsearch."""

CG_SESSION_WRITER = "cg:session-writer"
"""Consumer group: durable assistant message append to Postgres."""

CG_INSIGHTS = "cg:insights"
"""Consumer group: insights engine (Phase 3)."""

CG_PROMOTION = "cg:promotion"
"""Consumer group: promotion pipeline (Phase 3)."""

CG_CAPTAIN_LOG = "cg:captain-log"
"""Consumer group: captain's log reflection writer (Phase 3)."""

CG_FEEDBACK = "cg:feedback"
"""Consumer group: feedback signal consumers (Phase 3)."""

CG_FRESHNESS = "cg:freshness"
"""Consumer group: knowledge graph freshness tracker (Phase 4)."""

# Phase 2 stream names — ADR-0055 (System Health & Homeostasis)
STREAM_METRICS_SAMPLED = "stream:metrics.sampled"
"""Stream for metrics-sampled events (Phase 2 — ADR-0055)."""

STREAM_MODE_TRANSITION = "stream:mode.transition"
"""Stream for mode-transition events (Phase 2 — ADR-0055)."""

# Phase 2 consumer groups — ADR-0055
CG_MODE_CONTROLLER = "cg:mode-controller"
"""Consumer group: mode controller (Phase 2 — ADR-0055)."""

# Wave 2 — ADR-0056 (Error Pattern Monitoring)
STREAM_ERRORS_PATTERN_DETECTED = "stream:errors.pattern_detected"
"""Stream for error-pattern-detected events (Wave 2 — ADR-0056)."""

CG_ERROR_MONITOR = "cg:error-monitor"
"""Consumer group: error pattern monitor (Wave 2 — ADR-0056)."""


# ---------------------------------------------------------------------------
# Base model
# ---------------------------------------------------------------------------


class EventBase(BaseModel):
    """Base class for all event bus events (ADR-0041, ADR-0054).

    All feedback-stream contract fields live on this single base — ADR-0054
    decided against a secondary ``FeedbackEventBase`` root.  Subclasses that
    always carry a request trace (``RequestCaptured``, ``RequestCompleted``,
    ``MemoryAccessed``) narrow ``trace_id`` / ``session_id`` to required.
    Scheduled / system-triggered events leave them ``None``.

    Attributes:
        event_id: Unique identifier for this event instance.
        event_type: Literal discriminator — set by each concrete subclass.
        created_at: UTC timestamp when the event was created.
        trace_id: Request trace identifier the event is correlated with, or
            ``None`` for scheduled/system events (consolidation, idle,
            feedback poller).  Subclasses narrow to required where a trace
            always exists.
        session_id: Originating session id when available; ``None`` for
            system-level events with no session scope.
        source_component: Dotted module path of the emitting component
            (e.g. ``"request_gateway.monitoring"``).  Required so producer
            identity is visible independently of stream name.
        schema_version: Monotonically increasing integer; bumped when a
            field is added or semantics change.  Consumers tolerate any
            version — additive changes keep backward compatibility; breaking
            changes take a new ``event_type`` (ADR-0054 §D5).
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    event_type: str  # overridden as Literal in subclasses
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trace_id: str | None = None
    session_id: str | None = None
    source_component: str
    schema_version: int = 1


# ---------------------------------------------------------------------------
# Phase 1 events
# ---------------------------------------------------------------------------


class RequestCapturedEvent(EventBase):
    """Published after a task capture is written to disk.

    The consolidator consumer listens for this event to trigger
    near-real-time consolidation instead of relying on polling.

    Attributes:
        trace_id: Request trace identifier.
        session_id: Session that originated the request.
    """

    event_type: Literal["request.captured"] = "request.captured"
    trace_id: str
    session_id: str


class RequestCompletedEvent(EventBase):
    """Published when a chat request finishes (response ready).

    Consumers: ``cg:es-indexer`` (telemetry), ``cg:session-writer`` (DB append).
    Carries a timer snapshot for ES; identifiers only otherwise (ADR-0041).

    Attributes:
        trace_id: Request trace identifier.
        session_id: Session that originated the request.
        assistant_response: Assistant reply text to persist.
        trace_summary: Output of ``RequestTimer.to_trace_summary()`` at publish time.
        trace_breakdown: Output of ``RequestTimer.to_breakdown()`` at publish time.
    """

    event_type: Literal["request.completed"] = "request.completed"
    trace_id: str
    session_id: str
    assistant_response: str
    trace_summary: dict[str, Any]
    trace_breakdown: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Phase 3 events
# ---------------------------------------------------------------------------


class ConsolidationCompletedEvent(EventBase):
    """Published after second-brain consolidation finishes.

    Consumed by ``cg:insights`` (pattern analysis) and ``cg:promotion``
    (promote qualifying Captain's Log proposals to Linear).

    Attributes:
        captures_processed: Total captures examined in this run.
        entities_created: New Neo4j entities created.
        entities_promoted: Entities promoted from episodic to semantic memory.
    """

    event_type: Literal["consolidation.completed"] = "consolidation.completed"
    captures_processed: int
    entities_created: int
    entities_promoted: int


class PromotionIssueCreatedEvent(EventBase):
    """Published when the promotion pipeline creates a Linear issue.

    Consumed by ``cg:captain-log`` to write a reflection entry.

    Attributes:
        entry_id: Captain's Log entry ID that was promoted.
        linear_issue_id: Linear issue identifier (e.g. ``FRE-123``).
        fingerprint: Proposal fingerprint, if available.
    """

    event_type: Literal["promotion.issue_created"] = "promotion.issue_created"
    entry_id: str
    linear_issue_id: str
    fingerprint: str | None = None


class FeedbackReceivedEvent(EventBase):
    """Published after the feedback poller processes a Linear feedback label.

    Consumed by ``cg:insights`` (signal recording) and ``cg:feedback``
    (suppression updates for rejected proposals).

    Attributes:
        issue_id: Linear internal issue UUID.
        issue_identifier: Human-readable identifier (e.g. ``FRE-123``).
        label: The feedback label that was processed (e.g. ``Rejected``).
        fingerprint: Proposal fingerprint extracted from the issue description.
    """

    event_type: Literal["feedback.received"] = "feedback.received"
    issue_id: str
    issue_identifier: str
    label: str
    fingerprint: str | None = None


class SystemIdleEvent(EventBase):
    """Published by the brainstem scheduler when the system is idle.

    Consumed by ``cg:consolidator`` to trigger consolidation and by any
    future deferred-work consumers.

    Attributes:
        idle_seconds: Seconds since the last completed request.
        trigger: Source that determined idleness (default ``monitoring_loop``).
    """

    event_type: Literal["system.idle"] = "system.idle"
    idle_seconds: float
    trigger: str = "monitoring_loop"


# ---------------------------------------------------------------------------
# Phase 4 events
# ---------------------------------------------------------------------------


class AccessContext(str, Enum):
    """Discriminator for the access context of a memory query (ADR-0042).

    Each value identifies the subsystem that triggered the memory access,
    enabling the freshness consumer to prioritise updates and the insights
    engine to distinguish usage patterns.
    """

    SEARCH = "search"
    """Direct user-driven memory search (``query_memory`` / ``query_memory_broad``)."""

    CONTEXT_ASSEMBLY = "context_assembly"
    """Pre-LLM context assembly (``recall`` / ``recall_broad``)."""

    SUGGEST_RELEVANT = "suggest_relevant"
    """Proactive relevance suggestion (``suggest_relevant`` — ADR-0039)."""

    CONSOLIDATION = "consolidation"
    """Knowledge graph traversal during consolidation (``SecondBrainConsolidator``)."""

    TOOL_CALL = "tool_call"
    """``memory_search`` MCP tool invocation."""


class MemoryAccessedEvent(EventBase):
    """Published after a memory query operation completes (ADR-0042).

    Carries entity and relationship identifiers accessed during the query,
    along with a typed access context.  Consumed by ``cg:freshness`` to
    update ``last_accessed_at``, ``access_count``, and ``last_access_context``
    on the corresponding Neo4j nodes and relationships.

    Attributes:
        entity_ids: Neo4j entity IDs accessed during this query.
        relationship_ids: Neo4j relationship ``elementId`` values (Neo4j 5+) for
            edges read or created during this access (DISCUSSES, merges, etc.).
        access_context: Typed context that triggered the access.
        query_type: Fine-grained query method name (e.g. ``"recall"``,
            ``"recall_broad"``, ``"memory_search"``,
            ``"consolidation_traversal"``).
        trace_id: Request trace identifier for event correlation.
        session_id: Session that originated the request, if available.
    """

    event_type: Literal["memory.accessed"] = "memory.accessed"
    entity_ids: list[str]
    relationship_ids: list[str]
    access_context: AccessContext
    query_type: str
    trace_id: str
    session_id: str | None = None


class MemoryEntitiesUpdatedEvent(EventBase):
    """Published after consolidation updates entities in the knowledge graph.

    Carries the entity IDs that were updated (created or modified) during
    consolidation. Consumed by ``cg:freshness`` for knowledge graph
    freshness tracking.

    Attributes:
        entity_ids: List of Neo4j entity IDs that were created or updated.
        consolidation_id: Optional consolidation batch identifier.
    """

    event_type: Literal["memory.entities_updated"] = "memory.entities_updated"
    entity_ids: list[str]
    consolidation_id: str | None = None


# ---------------------------------------------------------------------------
# Phase 2 events — ADR-0055 (System Health & Homeostasis)
# ---------------------------------------------------------------------------


class MetricsSampledEvent(EventBase):
    """Published by MetricsDaemon every ~5 s with current hardware metrics.

    trace_id and session_id are None — system-scoped event, not request-scoped.
    source_component must be set to "brainstem.sensors.metrics_daemon".

    Attributes:
        sample_timestamp: UTC timestamp when the sample was collected.
        metrics: Raw output of ``poll_system_metrics()`` — keys use the
            ``perf_system_*`` / ``safety_*`` prefix convention (e.g.
            ``perf_system_cpu_load``, ``perf_system_mem_used``).
        sample_interval_seconds: Nominal poll cadence in seconds (default 5.0).
    """

    event_type: Literal["metrics.sampled"] = "metrics.sampled"
    sample_timestamp: datetime
    metrics: Mapping[str, float]
    sample_interval_seconds: float


class ModeTransitionEvent(EventBase):
    """Published by ModeManager on each FSM state transition.

    trace_id and session_id are None — system-scoped event, not request-scoped.
    source_component must be set to "brainstem.mode_manager".

    Attributes:
        from_mode: Mode the FSM was in before the transition.
        to_mode: Mode the FSM moved to.
        reason: Matched rule name or operator reason that triggered the transition.
        sensor_snapshot: Aggregated sensor values that triggered the rule, if
            available. Defaults to empty mapping.
        transition_index: Monotonic counter within the process lifetime; used by
            ``cg:mode-controller`` cadence tracking.
    """

    event_type: Literal["mode.transition"] = "mode.transition"
    from_mode: Mode
    to_mode: Mode
    reason: str
    sensor_snapshot: Mapping[str, float] = Field(default_factory=dict)
    transition_index: int


@dataclass(frozen=True)
class ErrorPatternCluster:
    """In-memory cluster of error events sharing a fingerprint (ADR-0056 Layer A).

    Built by ``ErrorMonitor.scan()`` from the ES composite aggregation.
    Used to construct ``ErrorPatternDetectedEvent`` for bus publication and
    the ``EP-<fingerprint>.json`` durable file.

    Attributes:
        fingerprint: sha256(component:event_name:error_type)[:16].
        component: Structlog logger/module (e.g. ``"tools.fetch_url"``).
        event_name: Structlog event name (e.g. ``"fetch_url_timeout"``).
        error_type: Normalised exception class or ``"<no_exc>"``.
        level: ``"ERROR"`` or ``"WARNING"``.
        occurrences: Count in the scan window.
        first_seen: Earliest timestamp in the window.
        last_seen: Most recent timestamp in the window.
        sample_trace_ids: Up to 5 representative trace IDs.
        sample_messages: Up to 3 distinct error messages.
        window_hours: Window that produced this cluster.
    """

    fingerprint: str
    component: str
    event_name: str
    error_type: str
    level: str
    occurrences: int
    first_seen: datetime
    last_seen: datetime
    sample_trace_ids: tuple[str, ...]
    sample_messages: tuple[str, ...]
    window_hours: int


class ErrorPatternDetectedEvent(EventBase):
    """Published by the error-monitor scan when a sustained error pattern is found.

    One event per cluster per scan.  ``trace_id`` and ``session_id`` are ``None``
    — scan is system-scoped, not request-correlated (ADR-0054 §D3).
    ``source_component`` defaults to ``"telemetry.error_monitor"``.

    Consumers:
      - ``cg:captain-log`` → ``CaptainLogEntry(category=RELIABILITY, scope=<derived>)``
      - Future: ``cg:context-quality`` (FRE-249), ``cg:skill-updater`` (FRE-226)

    Attributes:
        fingerprint: sha256(component:event_name:error_type)[:16].
        component: Structlog logger/module (e.g. "tools.fetch_url").
        event_name: Structlog event name (e.g. "fetch_url_timeout").
        error_type: Normalised exception class or ``"<no_exc>"``.
        level: ``"ERROR"`` or ``"WARNING"``.
        occurrences: Number of matching records in the scan window.
        first_seen: Earliest timestamp in the window.
        last_seen: Most recent timestamp in the window.
        window_hours: Size of the scan window that produced this cluster.
        sample_trace_ids: Up to 5 representative trace IDs.
        sample_messages: Up to 3 distinct error messages.
    """

    event_type: Literal["errors.pattern_detected"] = "errors.pattern_detected"
    source_component: str = "telemetry.error_monitor"
    trace_id: str | None = None

    fingerprint: str
    component: str
    event_name: str
    error_type: str
    level: str
    occurrences: int
    first_seen: datetime
    last_seen: datetime
    window_hours: int
    sample_trace_ids: list[str] = Field(default_factory=list)
    sample_messages: list[str] = Field(default_factory=list)

    @field_validator("sample_trace_ids", mode="before")
    @classmethod
    def _cap_trace_ids(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return v[:5]
        return list(v)[:5] if v else []

    @field_validator("sample_messages", mode="before")
    @classmethod
    def _cap_messages(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return v[:3]
        return list(v)[:3] if v else []


def parse_stream_event(payload: dict[str, Any]) -> EventBase:
    """Deserialize a stream JSON payload into the correct event subclass.

    ``EventBase.model_validate`` drops subclass fields; this dispatches on
    ``event_type`` so consumers receive full models.

    Args:
        payload: Decoded JSON object from the Redis ``data`` field.

    Returns:
        Concrete event instance.

    Raises:
        ValueError: If ``event_type`` is missing or unknown.
    """
    raw_type = payload.get("event_type")
    if raw_type == "request.captured":
        return RequestCapturedEvent.model_validate(payload)
    if raw_type == "request.completed":
        return RequestCompletedEvent.model_validate(payload)
    if raw_type == "consolidation.completed":
        return ConsolidationCompletedEvent.model_validate(payload)
    if raw_type == "promotion.issue_created":
        return PromotionIssueCreatedEvent.model_validate(payload)
    if raw_type == "feedback.received":
        return FeedbackReceivedEvent.model_validate(payload)
    if raw_type == "system.idle":
        return SystemIdleEvent.model_validate(payload)
    if raw_type == "memory.accessed":
        return MemoryAccessedEvent.model_validate(payload)
    if raw_type == "memory.entities_updated":
        return MemoryEntitiesUpdatedEvent.model_validate(payload)
    if raw_type == "metrics.sampled":
        return MetricsSampledEvent.model_validate(payload)
    if raw_type == "mode.transition":
        return ModeTransitionEvent.model_validate(payload)
    if raw_type == "errors.pattern_detected":
        return ErrorPatternDetectedEvent.model_validate(payload)
    raise ValueError(f"unknown event_type: {raw_type!r}")
