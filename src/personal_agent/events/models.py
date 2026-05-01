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

# Wave 2 — ADR-0057 (Insights & Pattern Analysis)
STREAM_INSIGHTS_PATTERN_DETECTED = "stream:insights.pattern_detected"
"""Stream for insights-pattern-detected events (Wave 2 — ADR-0057)."""

STREAM_INSIGHTS_COST_ANOMALY = "stream:insights.cost_anomaly"
"""Stream for insights-cost-anomaly events (Wave 2 — ADR-0057)."""

# Wave 2 — ADR-0058 (Self-Improvement Pipeline Stream)
STREAM_CAPTAIN_LOG_ENTRY_CREATED = "stream:captain_log.entry_created"
"""Stream for captain-log-entry-created events (Wave 2 — ADR-0058).

Producer: ``CaptainLogManager.save_entry()`` / ``_merge_into_existing()``.
Fires on every successful durable write — both first writes (``is_merge=False``)
and dedup merges (``is_merge=True``).  Suppressed entries (ADR-0040 rejection)
do not fire.  Ordering rule: durable file write must succeed before publish
(ADR-0054 D4).  Bus failures are logged and swallowed (ADR-0054 D6).
"""

# Wave 3 — ADR-0059 (Context Quality Stream)
STREAM_CONTEXT_COMPACTION_QUALITY_POOR = "stream:context.compaction_quality_poor"

# Wave 3 — ADR-0060 (Knowledge Graph Quality Stream)
STREAM_GRAPH_QUALITY_ANOMALY = "stream:graph.quality_anomaly"
"""Stream for graph-quality-anomaly events (Wave 3 — ADR-0060 Stream 8).

Producer: ``BrainstemScheduler._run_quality_monitoring()``.  One event per
detected anomaly per daily run.  Consumer ``cg:graph-monitor`` writes
``CaptainLogEntry(category=RELIABILITY|KNOWLEDGE_QUALITY, scope=SECOND_BRAIN)``.
Ordering rule: durable JSONL append before publish (ADR-0054 D4).
"""

STREAM_MEMORY_STALENESS_REVIEWED = "stream:memory.staleness_reviewed"
"""Stream for memory-staleness-reviewed events (Wave 3 — ADR-0060 Stream 6).

Producer: ``brainstem.jobs.freshness_review.run_freshness_review()``.  One event
per weekly review run.  Consumer ``cg:graph-monitor`` writes a trend-summary
``CaptainLogEntry`` when dormant entity count exceeds threshold.
"""

CG_GRAPH_MONITOR = "cg:graph-monitor"
"""Consumer group: knowledge graph quality monitor (Wave 3 — ADR-0060)."""

# Wave 4 — ADR-0061 (Within-Session Progressive Context Compression)
STREAM_CONTEXT_WITHIN_SESSION_COMPRESSED = "stream:context.within_session_compressed"
"""Stream for within-session compression events (Wave 4 — ADR-0061).

Producer: ``orchestrator.within_session_compression.compress_in_place`` via
``telemetry.within_session_compression.record_compression``.  One event per
within-session compression pass (soft async or hard synchronous).  No consumer
in Phase 1 — observability only.  Phase 2 will subscribe a tuning consumer
that adapts ``min_tail_tokens`` / ``pre_pass_threshold_tokens`` from the
ADR-0059 per-session signal.  Ordering rule: durable JSONL append before
publish (ADR-0054 D4).
"""


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


class InsightsPatternDetectedEvent(EventBase):
    """Published when InsightsEngine.analyze_patterns() produces an insight (ADR-0057).

    One event per insight per consolidation. trace_id and session_id are
    None — consolidation-triggered, not request-correlated.

    Consumers:
      - cg:captain-log reaches CL via create_captain_log_proposals() in the
        producer handler; this event is for FUTURE consumers that want to act
        on patterns without touching the proposal pipeline.
      - FRE-250 (KG Quality) filters on insight_type in
        {"graph_staleness", "graph_staleness_trend"}.

    Attributes:
        insight_type: One of the 6 built-in types or "delegation".
        pattern_kind: Discriminator within insight_type (e.g.
            "delegation_success_rate"). Empty string for the 6 built-ins.
        title: Short human-readable title.
        summary: Multi-line summary.
        confidence: 0..1 confidence score.
        actionable: Whether this insight warrants a proposal.
        evidence: Structured evidence fields.
        fingerprint: sha256(insight_type:pattern_kind:normalised_title)[:16].
        analysis_window_days: Lookback window used to generate the insight.
    """

    event_type: Literal["insights.pattern_detected"] = "insights.pattern_detected"
    source_component: str = "insights.engine"
    trace_id: str | None = None
    session_id: str | None = None

    insight_type: str
    pattern_kind: str
    title: str
    summary: str
    confidence: float
    actionable: bool
    evidence: dict[str, float | int | str]
    fingerprint: str
    analysis_window_days: int


class InsightsCostAnomalyEvent(EventBase):
    """Published when InsightsEngine.detect_cost_anomalies() detects a spike (ADR-0057).

    Separate from pattern events because the response path is fundamentally
    different — patterns propose self-improvement; cost anomalies may trigger
    governance responses (Phase 2, deferred).

    Attributes:
        anomaly_type: Today only "daily_cost_spike".
        message: Human-readable description.
        observed_cost_usd: Observed daily cost that triggered the anomaly.
        baseline_cost_usd: Rolling baseline mean.
        ratio: observed / baseline.
        confidence: 0..1 confidence score.
        severity: "low" | "medium" | "high".
        fingerprint: sha256(anomaly_type:observation_date)[:16].
        observation_date: ISO yyyy-mm-dd of the day that spiked.
    """

    event_type: Literal["insights.cost_anomaly"] = "insights.cost_anomaly"
    source_component: str = "insights.engine"
    trace_id: str | None = None
    session_id: str | None = None

    anomaly_type: str
    message: str
    observed_cost_usd: float
    baseline_cost_usd: float
    ratio: float
    confidence: float
    severity: Literal["low", "medium", "high"]
    fingerprint: str
    observation_date: str


class CaptainLogEntryCreatedEvent(EventBase):
    """Published after a Captain's Log entry is durably written (ADR-0058).

    Fires from ``CaptainLogManager.save_entry()`` and
    ``CaptainLogManager._merge_into_existing()`` — the two persist sites that
    all CL construction call sites funnel through.  Suppressed entries
    (ADR-0040 rejection fingerprint) do **not** fire this event.

    ``trace_id`` and ``session_id`` are ``None`` for scheduled / system-scoped
    entries (consolidation insights, freshness review, mode-controller
    proposals).  They are populated for task-reflection entries where a
    request trace is available.

    ``source_component`` defaults to ``"captains_log.manager"`` — the single
    producer of this event.

    Attributes:
        entry_id: Captain's Log entry identifier (``CL-<date>-<hash>`` form).
        entry_type: ``CaptainLogEntryType.value`` string (e.g. ``"REFLECTION"``).
        title: Short human-readable title from the CL entry.
        fingerprint: Proposal fingerprint from ``ProposedChange.fingerprint``,
            or ``None`` if the entry has no proposal.
        seen_count: Dedup count at write time.  ``1`` for a first write;
            ``≥ 2`` for a merge that incremented an existing entry.
        is_merge: ``True`` when this write was a dedup merge; ``False`` for a
            first write.
        category: ``ChangeCategory.value`` string (e.g. ``"performance"``,
            ``"reliability"``) if the entry carries a proposed change, else
            ``None``.
        scope: ``ChangeScope.value`` string (e.g. ``"orchestrator"``,
            ``"captains_log"``) if the entry carries a proposed change, else
            ``None``.
    """

    event_type: Literal["captain_log.entry_created"] = "captain_log.entry_created"
    source_component: str = "captains_log.manager"

    entry_id: str
    entry_type: str
    title: str
    fingerprint: str | None = None
    seen_count: int = 1
    is_merge: bool = False
    category: str | None = None
    scope: str | None = None


# ---------------------------------------------------------------------------
# Wave 3 events — ADR-0059 (Context Quality Stream)
# ---------------------------------------------------------------------------


class CompactionQualityIncidentEvent(EventBase):
    """Published when the recall controller detects a poor-compaction incident.

    One event per detection — fires inline in Stage 4b when a noun phrase
    extracted from the user message substring-matches an entity that Stage 7
    dropped earlier in the same session (ADR-0047 D3, ADR-0059).

    Consumers:
      - ``cg:captain-log`` →
        ``CaptainLogEntry(category=KNOWLEDGE_QUALITY, scope=ORCHESTRATOR)``.
      - Phase 2 (flag-gated): in-process ``IncidentTracker`` per-session
        counter consumed by Stage 7 budget hook to tighten ``max_tokens``.

    The ADR-0056 cluster path (``compaction_quality.poor`` warning →
    ``ErrorPatternDetectedEvent``) and this per-incident path produce
    distinct fingerprints by construction; ADR-0030 fingerprint dedup at
    ``CaptainLogManager.save_entry()`` merges any overlap cleanly.

    Attributes:
        fingerprint: sha256(noun_phrase:dropped_entity:component)[:16].
        noun_phrase: Cue extracted from the user message that triggered match.
        dropped_entity: Identifier of the entity dropped earlier in the
            session by Stage 7 compaction.
        recall_cue: Regex cue from ``_RECALL_CUE_PATTERNS`` that engaged the
            recall controller.
        tier_affected: Compaction tier the dropped entity originated from
            (``"near"`` | ``"episodic"`` | ``"long_term"``).
        tokens_removed: Tokens removed by the originating compaction event;
            ``0`` when not available at detection time.
        detected_at: UTC timestamp when the incident was detected.
    """

    event_type: Literal["context.compaction_quality_poor"] = "context.compaction_quality_poor"
    source_component: str = "telemetry.context_quality"

    fingerprint: str
    noun_phrase: str
    dropped_entity: str
    recall_cue: str
    tier_affected: str
    tokens_removed: int = 0
    detected_at: datetime


# ---------------------------------------------------------------------------
# Wave 3 — ADR-0060 (Knowledge Graph Quality Stream)
# ---------------------------------------------------------------------------


class GraphQualityAnomalyEvent(EventBase):
    """Published per anomaly when the daily quality monitor fires (ADR-0060 Stream 8).

    One event per detected anomaly.  ``trace_id`` and ``session_id`` are
    ``None`` — scheduled event, not request-correlated.
    ``source_component`` must be set to ``"brainstem.scheduler"``.

    Consumers:
      - ``cg:graph-monitor`` → ``CaptainLogEntry(severity-gated category, SECOND_BRAIN)``
      - Phase 2 (flag-gated): high-severity → ``ModeAdvisoryEvent`` on
        ``stream:mode.transition``

    Attributes:
        fingerprint: sha256(graph_quality:anomaly_type:normalised_message)[:16].
        anomaly_type: One of the six quality-monitor anomaly types.
        severity: ``"high"`` or ``"medium"``.
        message: Human-readable anomaly description.
        observed_value: Numeric value that triggered the anomaly.
        expected_range: (low, high) target range, or ``None`` for spike detection.
        metadata: Optional extra context from the detector.
        observation_date: ISO yyyy-mm-dd of the day the monitor ran.
    """

    event_type: Literal["graph.quality_anomaly"] = "graph.quality_anomaly"
    source_component: str = "brainstem.scheduler"
    trace_id: str | None = None
    session_id: str | None = None

    fingerprint: str
    anomaly_type: str
    severity: Literal["high", "medium"]
    message: str
    observed_value: float
    expected_range: tuple[float, float] | None = None
    metadata: dict[str, Any] | None = None
    observation_date: str


class MemoryStalenessReviewedEvent(EventBase):
    """Published once per weekly freshness review run (ADR-0060 Stream 6).

    ``trace_id`` and ``session_id`` are ``None`` — scheduled event.
    ``source_component`` must be ``"brainstem.jobs.freshness_review"``.

    Consumers:
      - ``cg:graph-monitor`` → trend-summary ``CaptainLogEntry`` when
        ``entities_dormant ≥ settings.freshness_dormant_entity_proposal_threshold``.

    Attributes:
        fingerprint: sha256(staleness_review_<dominant_tier>:<iso_week>)[:16].
        iso_week: ISO week string, e.g. ``"2026-W18"``.
        entities_warm: Count of entities in WARM tier.
        entities_cooling: Count of entities in COOLING tier.
        entities_cold: Count of entities in COLD tier.
        entities_dormant: Count of entities in DORMANT tier.
        relationships_dormant: Count of dormant relationships.
        never_accessed_old_entity_count: Entities never accessed and older than
            ``cold_threshold_days``.
        dominant_tier: ``"dormant"`` | ``"cold"`` | ``"cooling"`` | ``"warm"``.
    """

    event_type: Literal["memory.staleness_reviewed"] = "memory.staleness_reviewed"
    source_component: str = "brainstem.jobs.freshness_review"
    trace_id: str | None = None
    session_id: str | None = None

    fingerprint: str
    iso_week: str
    entities_warm: int
    entities_cooling: int
    entities_cold: int
    entities_dormant: int
    relationships_dormant: int
    never_accessed_old_entity_count: int
    dominant_tier: str


class WithinSessionCompressionEvent(EventBase):
    """Published per within-session compression pass (Wave 4 — ADR-0061).

    One event per compression pass — either the soft async path
    (``maybe_trigger_compression``) or the hard synchronous path inside the
    orchestrator loop.  Phase 1 has no consumer; the bus publish is
    observability + a composability hook for Phase 2's adaptive tuning
    consumer.

    ``trace_id`` and ``session_id`` are required — every compression is
    request-correlated.  ``source_component`` defaults to
    ``"orchestrator.within_session_compression"``.

    Attributes:
        trigger: ``"soft"`` (async, between turns) or ``"hard"``
            (synchronous, mid-orchestration).
        head_tokens: Tokens preserved in the head (system + first user msg).
        middle_tokens_in: Tokens in the middle band before pre-pass and LLM.
        middle_tokens_out: Tokens in the middle after pre-pass + LLM
            summariser.  Equals the summary token count when summariser ran;
            equals the pre-pass-only middle when summariser was skipped or
            failed.
        tail_tokens: Tokens preserved in the tail (last K messages).
        pre_pass_replacements: Number of large tool messages replaced with
            descriptors during the pre-pass step.
        summariser_called: Whether the LLM compressor was invoked.  ``False``
            when pre-pass alone reduced the middle below threshold or when
            the compressor role was missing.
        summariser_duration_ms: Wall time of the compressor call when
            invoked; ``0`` otherwise.
        tokens_saved: ``middle_tokens_in - middle_tokens_out`` (always ≥ 0).
    """

    event_type: Literal[
        "context.within_session_compressed"
    ] = "context.within_session_compressed"
    source_component: str = "orchestrator.within_session_compression"

    trace_id: str
    session_id: str
    trigger: Literal["soft", "hard"]
    head_tokens: int
    middle_tokens_in: int
    middle_tokens_out: int
    tail_tokens: int
    pre_pass_replacements: int
    summariser_called: bool
    summariser_duration_ms: int
    tokens_saved: int


class ModeAdvisoryEvent(EventBase):
    """Published by quality monitors to advise the brainstem of a suggested mode (ADR-0060 §D7).

    Published to ``stream:mode.transition`` so the mode controller can observe
    the advisory in its rolling window. Phase 2 of ADR-0060 — default flag off
    (``graph_quality_governance_enabled=False``).

    ``trace_id`` and ``session_id`` are ``None`` — system-scoped event.

    Attributes:
        target_mode: Suggested mode (e.g. ``"degraded"``).
        surface_tag: Subsystem tag to scope the advisory (e.g. ``"consolidation"``).
        reason: Human-readable reason (e.g. ``"graph_quality_anomaly:entity_extraction_spike"``).
    """

    event_type: Literal["mode.advisory"] = "mode.advisory"
    source_component: str = "events.pipeline_handlers"
    trace_id: str | None = None
    session_id: str | None = None

    target_mode: str
    surface_tag: str
    reason: str


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
    if raw_type == "insights.pattern_detected":
        return InsightsPatternDetectedEvent.model_validate(payload)
    if raw_type == "insights.cost_anomaly":
        return InsightsCostAnomalyEvent.model_validate(payload)
    if raw_type == "captain_log.entry_created":
        return CaptainLogEntryCreatedEvent.model_validate(payload)
    if raw_type == "context.compaction_quality_poor":
        return CompactionQualityIncidentEvent.model_validate(payload)
    if raw_type == "graph.quality_anomaly":
        return GraphQualityAnomalyEvent.model_validate(payload)
    if raw_type == "memory.staleness_reviewed":
        return MemoryStalenessReviewedEvent.model_validate(payload)
    if raw_type == "mode.advisory":
        return ModeAdvisoryEvent.model_validate(payload)
    if raw_type == "context.within_session_compressed":
        return WithinSessionCompressionEvent.model_validate(payload)
    raise ValueError(f"unknown event_type: {raw_type!r}")
