"""Event models for the Redis Streams event bus (ADR-0041).

All event models are frozen Pydantic models with a ``event_type`` literal
discriminator — consistent with the project's discriminated-union coding
standard.  Events carry identifiers and metadata, not large payloads;
consumers fetch full data from the source system when needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

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


# ---------------------------------------------------------------------------
# Base model
# ---------------------------------------------------------------------------


class EventBase(BaseModel):
    """Base class for all event bus events.

    Attributes:
        event_id: Unique identifier for this event instance.
        event_type: Literal discriminator — set by each concrete subclass.
        created_at: UTC timestamp when the event was created.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    event_type: str  # overridden as Literal in subclasses
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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


class MemoryAccessedEvent(EventBase):
    """Published after a memory query operation completes.

    Carries entity identifiers accessed during the query, along with the
    query context (search, consolidation, context-assembly, etc.).
    Consumed by ``cg:freshness`` (no-op stub in Phase 4; follow-on ADR
    designs the actual knowledge graph freshness consumer).

    Attributes:
        entity_ids: List of Neo4j entity IDs accessed during this query.
        query_context: Context where the query occurred (e.g., ``"search"``,
            ``"consolidation"``, ``"context_assembly"``).
        trace_id: Request trace identifier, if available.
    """

    event_type: Literal["memory.accessed"] = "memory.accessed"
    entity_ids: list[str]
    query_context: str
    trace_id: str | None = None


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
    raise ValueError(f"unknown event_type: {raw_type!r}")
