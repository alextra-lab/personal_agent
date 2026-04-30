"""Consumer handlers for Phase 3 pipeline decoupling (FRE-159 / ADR-0041 Phase 3).

Each builder returns an async handler suitable for ``EventBus.subscribe()``.
Handlers are pure functions over the event bus — they import heavy dependencies
lazily so the module loads cheaply and stays testable with lightweight mocks.
"""

from __future__ import annotations

from typing import Any

from personal_agent.captains_log.models import ChangeScope
from personal_agent.events.models import (
    CompactionQualityIncidentEvent,
    ConsolidationCompletedEvent,
    ErrorPatternDetectedEvent,
    EventBase,
    FeedbackReceivedEvent,
    GraphQualityAnomalyEvent,
    MemoryStalenessReviewedEvent,
    PromotionIssueCreatedEvent,
)
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


def _default_elasticsearch_async_client() -> Any | None:
    """Return the app's AsyncElasticsearch client when Captain's Log ES is wired.

    Avoids creating a second :class:`~elasticsearch.AsyncElasticsearch` (and
    aiohttp pool) for :class:`~personal_agent.telemetry.queries.TelemetryQueries`
    when the main process already has a connected handler.

    Returns:
        The shared async client, or ``None`` if unavailable.
    """
    try:
        from personal_agent.captains_log.manager import CaptainLogManager

        handler = CaptainLogManager._default_es_handler
        if handler is None or not getattr(handler, "_connected", False):
            return None
        return getattr(handler.es_logger, "client", None)
    except Exception:
        return None


def build_consolidation_insights_handler(
    memory_service: Any | None = None,
    event_bus: Any | None = None,
) -> Any:
    """Build handler that runs insights analysis on ``consolidation.completed``.

    On each event (when captures_processed > 0 and insights_wiring_enabled):
      1. Calls ``InsightsEngine.analyze_patterns(days=7)``.
      2. Publishes one ``InsightsPatternDetectedEvent`` per ``Insight``
         to ``stream:insights.pattern_detected``.
      3. Publishes one ``InsightsCostAnomalyEvent`` per anomaly insight
         to ``stream:insights.cost_anomaly``.
      4. Calls ``InsightsEngine.create_captain_log_proposals(insights)``.
      5. Saves each proposal via ``CaptainLogManager.save_entry()``
         (ADR-0030 fingerprint dedup + ADR-0040 suppression apply).

    All bus publishes and CL saves are best-effort — exceptions are logged
    and swallowed so a single failure never poisons the scan.

    Args:
        memory_service: Optional connected MemoryService for graph-backed insights.
        event_bus: Optional EventBus override (used in tests). When None, the
            handler lazily fetches ``get_event_bus()`` at invocation time.

    Returns:
        Async handler for ``cg:insights`` on ``stream:consolidation.completed``.
    """

    async def handler(event: EventBase) -> None:
        if not isinstance(event, ConsolidationCompletedEvent):
            return
        if not event.captures_processed:
            log.debug(
                "insights_analysis_skipped_no_captures",
                event_id=event.event_id,
            )
            return
        from personal_agent.config.settings import get_settings
        from personal_agent.insights.engine import InsightsEngine
        from personal_agent.telemetry.queries import TelemetryQueries

        shared_es = _default_elasticsearch_async_client()
        queries = TelemetryQueries(es_client=shared_es)
        engine = InsightsEngine(telemetry_queries=queries, memory_service=memory_service)
        try:
            insights = await engine.analyze_patterns(days=7)
            log.info(
                "insights_analysis_from_consolidation",
                event_id=event.event_id,
                captures_processed=event.captures_processed,
                insights_count=len(insights),
            )

            # Short-circuit: no signals or wiring disabled → nothing to publish.
            # This keeps existing tests (which mock analyze_patterns → []) passing
            # without requiring them to mock create_captain_log_proposals.
            if not insights or not get_settings().insights_wiring_enabled:
                return

            # Resolve bus lazily when not injected (production path)
            bus = event_bus
            if bus is None:
                from personal_agent.events.bus import get_event_bus

                bus = get_event_bus()

            await _publish_insight_events(bus, insights)

            proposals = await engine.create_captain_log_proposals(insights)
            await _save_proposals(proposals)
        finally:
            await queries.disconnect()

    return handler


async def _publish_insight_events(bus: Any, insights: list[Any]) -> None:
    """Publish InsightsPatternDetectedEvent + InsightsCostAnomalyEvent per insight.

    Each insight produces a pattern event. Insights where ``insight_type == "anomaly"``
    additionally produce a typed cost anomaly event with structured numeric fields.

    Args:
        bus: EventBus instance to publish on.
        insights: List of Insight objects from InsightsEngine.analyze_patterns().
    """
    from datetime import datetime, timezone

    from personal_agent.events.models import (
        STREAM_INSIGHTS_COST_ANOMALY,
        STREAM_INSIGHTS_PATTERN_DETECTED,
        InsightsCostAnomalyEvent,
        InsightsPatternDetectedEvent,
    )
    from personal_agent.insights.fingerprints import (
        cost_fingerprint,
        pattern_fingerprint,
        severity_for_cost_ratio,
    )

    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for insight in insights:
        pattern_kind = getattr(insight, "pattern_kind", "") or ""
        fingerprint = pattern_fingerprint(insight.insight_type, pattern_kind, insight.title)
        try:
            await bus.publish(
                STREAM_INSIGHTS_PATTERN_DETECTED,
                InsightsPatternDetectedEvent(
                    source_component="insights.engine",
                    insight_type=insight.insight_type,
                    pattern_kind=pattern_kind,
                    title=insight.title,
                    summary=insight.summary,
                    confidence=float(insight.confidence),
                    actionable=bool(insight.actionable),
                    evidence=dict(insight.evidence),
                    fingerprint=fingerprint,
                    analysis_window_days=7,
                ),
            )
        except Exception as exc:
            log.warning(
                "insights_pattern_event_publish_failed",
                error=str(exc),
                insight_type=insight.insight_type,
            )

        if insight.insight_type == "anomaly":
            evidence = dict(insight.evidence)
            ratio = float(evidence.get("ratio", 0.0))
            try:
                await bus.publish(
                    STREAM_INSIGHTS_COST_ANOMALY,
                    InsightsCostAnomalyEvent(
                        source_component="insights.engine",
                        anomaly_type="daily_cost_spike",
                        message=insight.summary,
                        observed_cost_usd=float(evidence.get("observed_cost_usd", 0.0)),
                        baseline_cost_usd=float(evidence.get("baseline_cost_usd", 0.0)),
                        ratio=ratio,
                        confidence=float(insight.confidence),
                        severity=severity_for_cost_ratio(ratio),
                        fingerprint=cost_fingerprint("daily_cost_spike", today_iso),
                        observation_date=today_iso,
                    ),
                )
            except Exception as exc:
                log.warning(
                    "insights_cost_anomaly_event_publish_failed",
                    error=str(exc),
                )


async def _save_proposals(proposals: list[Any]) -> None:
    """Save each CaptainLogEntry proposal via CaptainLogManager (best-effort).

    ADR-0030 fingerprint dedup and ADR-0040 suppression are applied by
    CaptainLogManager.save_entry() — no additional handling needed here.

    Args:
        proposals: List of CaptainLogEntry objects from create_captain_log_proposals().
    """
    if not proposals:
        return
    from personal_agent.captains_log.manager import CaptainLogManager

    manager = CaptainLogManager()
    for proposal in proposals:
        try:
            manager.save_entry(proposal)
        except Exception as exc:
            log.warning(
                "insights_captain_log_save_failed",
                error=str(exc),
                proposal_title=getattr(proposal, "title", None),
            )


def build_consolidation_promotion_handler(
    linear_client: Any | None = None,
    criteria: Any | None = None,
) -> Any:
    """Build handler that runs the promotion pipeline on ``consolidation.completed``.

    Args:
        linear_client: Optional Linear MCP client for budget and duplicate checks.
        criteria: Optional ``PromotionCriteria`` override.

    Returns:
        Async handler for ``cg:promotion`` on ``stream:consolidation.completed``.
    """

    async def handler(event: EventBase) -> None:
        if not isinstance(event, ConsolidationCompletedEvent):
            return
        from personal_agent.captains_log.promotion import PromotionCriteria, PromotionPipeline
        from personal_agent.config.settings import get_settings

        _settings = get_settings()
        promo_criteria = criteria or PromotionCriteria(
            max_existing_linear_issues=_settings.promotion_initial_cap,
        )
        pipeline = PromotionPipeline(
            criteria=promo_criteria,
            linear_client=linear_client,
            create_issue_fn=(linear_client.create_issue if linear_client else None),
        )
        promoted = await pipeline.run()
        log.info(
            "promotion_from_consolidation",
            event_id=event.event_id,
            promoted_count=len(promoted),
        )

    return handler


def build_promotion_captain_log_handler() -> Any:
    """Build handler that saves a Captain's Log reflection on ``promotion.issue_created``.

    Creates a brief ``OBSERVATION`` entry confirming which Captain's Log
    proposal was promoted and to which Linear issue.

    Returns:
        Async handler for ``cg:captain-log`` on ``stream:promotion.issue_created``.
    """

    async def handler(event: EventBase) -> None:
        if not isinstance(event, PromotionIssueCreatedEvent):
            return
        from datetime import datetime, timezone

        from personal_agent.captains_log.manager import CaptainLogManager
        from personal_agent.captains_log.models import (
            CaptainLogEntry,
            CaptainLogEntryType,
            CaptainLogStatus,
        )

        now = datetime.now(timezone.utc)
        entry = CaptainLogEntry(
            entry_id=f"CL-{now.strftime('%Y%m%d-%H%M%S')}-promo-{event.entry_id[-6:]}",
            type=CaptainLogEntryType.OBSERVATION,
            title=f"Proposal promoted to Linear: {event.linear_issue_id}",
            rationale=(
                f"Captain's Log entry {event.entry_id!r} was promoted to Linear issue "
                f"{event.linear_issue_id!r} by the promotion pipeline (ADR-0030). "
                f"Fingerprint: {event.fingerprint or 'n/a'}."
            ),
            status=CaptainLogStatus.APPROVED,
            proposed_change=None,
            metrics_structured=None,
            impact_assessment=None,
            reviewer_notes=None,
            linear_issue_id=None,
            experiment_design=None,
            expected_outcome=None,
            potential_implementation=None,
        )
        manager = CaptainLogManager()
        manager.save_entry(entry)
        log.info(
            "captain_log_promotion_reflection_saved",
            entry_id=event.entry_id,
            linear_issue_id=event.linear_issue_id,
        )

    return handler


def build_feedback_insights_handler() -> Any:
    """Build handler that records a feedback signal for insights on ``feedback.received``.

    Logs the signal for future targeted analysis.  Extended in Slice 3 to
    trigger adaptive insights re-analysis on high-signal feedback (e.g.
    multiple rejections in a short window).

    Returns:
        Async handler for ``cg:insights`` on ``stream:feedback.received``.
    """

    async def handler(event: EventBase) -> None:
        if not isinstance(event, FeedbackReceivedEvent):
            return
        log.info(
            "feedback_signal_received_for_insights",
            event_id=event.event_id,
            issue_identifier=event.issue_identifier,
            label=event.label,
            fingerprint=event.fingerprint,
        )

    return handler


def build_feedback_suppression_handler() -> Any:
    """Build handler that updates promotion suppression on ``feedback.received``.

    Only acts when the label is ``Rejected`` and a fingerprint is available.
    The call is idempotent — recording suppression twice for the same
    fingerprint overwrites with an extended window, which is safe.

    Returns:
        Async handler for ``cg:feedback`` on ``stream:feedback.received``.
    """

    async def handler(event: EventBase) -> None:
        if not isinstance(event, FeedbackReceivedEvent):
            return
        if event.label != "Rejected":
            return
        if not event.fingerprint:
            return
        from personal_agent.captains_log.suppression import record_rejection_suppression

        record_rejection_suppression(
            event.fingerprint,
            issue_identifier=event.issue_identifier,
        )
        log.info(
            "feedback_suppression_updated",
            event_id=event.event_id,
            issue_identifier=event.issue_identifier,
            fingerprint=event.fingerprint,
        )

    return handler


# ---------------------------------------------------------------------------
# Error-pattern → Captain's Log handler (ADR-0056 §D5)
# ---------------------------------------------------------------------------

# Prefix → ChangeScope mapping per ADR-0056 §D5.  First matching prefix wins.
_COMPONENT_TO_SCOPE: list[tuple[str, ChangeScope]] = [
    ("tools.", ChangeScope.TOOLS),
    ("mcp.", ChangeScope.TOOLS),
    ("orchestrator.", ChangeScope.ORCHESTRATOR),
    ("request_gateway.", ChangeScope.ORCHESTRATOR),
    ("memory.", ChangeScope.SECOND_BRAIN),
    ("second_brain.", ChangeScope.SECOND_BRAIN),
    ("captains_log.", ChangeScope.CAPTAINS_LOG),
    ("brainstem.", ChangeScope.BRAINSTEM),
    ("telemetry.", ChangeScope.TELEMETRY),
    ("governance.", ChangeScope.GOVERNANCE),
    ("insights.", ChangeScope.INSIGHTS),
    ("llm_client.", ChangeScope.LLM_CLIENT),
]


def _scope_from_component(component: str) -> ChangeScope:
    """Derive ChangeScope from a component path per ADR-0056 §D5."""
    for prefix, scope in _COMPONENT_TO_SCOPE:
        if component.startswith(prefix):
            return scope
    return ChangeScope.CROSS_CUTTING


def build_error_pattern_captain_log_handler(manager: Any | None = None) -> Any:
    """Build handler that writes a Captain's Log entry on ``errors.pattern_detected``.

    Subscribes ``cg:captain-log`` to ``stream:errors.pattern_detected``.  Each
    ``ErrorPatternDetectedEvent`` becomes a ``CONFIG_PROPOSAL`` entry with
    ``category=RELIABILITY`` and ``scope`` derived from the component prefix.

    Dedup and suppression are handled by the existing ``CaptainLogManager``
    infrastructure (ADR-0030): passing the same fingerprint increments
    ``seen_count`` rather than creating a duplicate entry.

    Args:
        manager: Optional ``CaptainLogManager`` instance.  When ``None``, a
            fresh instance is created lazily inside the handler.

    Returns:
        Async handler for ``cg:captain-log`` on ``stream:errors.pattern_detected``.
    """

    async def handler(event: EventBase) -> None:
        if not isinstance(event, ErrorPatternDetectedEvent):
            return

        from datetime import datetime, timezone

        from personal_agent.captains_log.manager import CaptainLogManager
        from personal_agent.captains_log.models import (
            CaptainLogEntry,
            CaptainLogEntryType,
            ChangeCategory,
            Metric,
            ProposedChange,
            TelemetryRef,
        )

        _manager = manager or CaptainLogManager()
        now = datetime.now(timezone.utc)
        scope = _scope_from_component(event.component)

        entry = CaptainLogEntry(
            entry_id=f"CL-{now.strftime('%Y%m%d-%H%M%S')}-ep-{event.fingerprint[:6]}",
            type=CaptainLogEntryType.CONFIG_PROPOSAL,
            title=(
                f"Error pattern: {event.event_name} in {event.component} "
                f"({event.occurrences}x/{event.window_hours}h)"
            ),
            rationale=(
                f"{event.occurrences} occurrences of `{event.event_name}` in "
                f"`{event.component}` over the last {event.window_hours} hours "
                f"(error_type={event.error_type}). "
                f"Sample traces: {list(event.sample_trace_ids)}. "
                f"Representative messages: {list(event.sample_messages)}."
            ),
            proposed_change=ProposedChange(
                what=(f"Investigate and mitigate repeated {event.event_name} in {event.component}"),
                why=(
                    "Sustained error pattern detected by Level 3 self-observability. "
                    "Repeated failures of this class degrade the capability served "
                    f"by {event.component}."
                ),
                how=(
                    "1) Open representative traces in Kibana to understand the "
                    "immediate cause.\n"
                    "2) Decide whether the fix is a retry/backoff policy, a guard, "
                    "a schema change, or a tool description update.\n"
                    "3) If Phase 2 failure-path reflection is enabled, the surgical "
                    "edit suggestion is attached in `potential_implementation`."
                ),
                category=ChangeCategory.RELIABILITY,
                scope=scope,
                fingerprint=event.fingerprint,
                first_seen=None,
            ),
            supporting_metrics=[
                f"occurrences: {event.occurrences}",
                f"window_hours: {event.window_hours}",
                f"first_seen: {event.first_seen.isoformat()}",
                f"last_seen: {event.last_seen.isoformat()}",
            ],
            metrics_structured=[
                Metric(name="occurrences", value=event.occurrences, unit="count"),
                Metric(name="window_hours", value=event.window_hours, unit="h"),
            ],
            telemetry_refs=[
                TelemetryRef(trace_id=tid, metric_name=None, value=None)
                for tid in event.sample_trace_ids
            ],
            impact_assessment=None,
            reviewer_notes=None,
            linear_issue_id=None,
            experiment_design=None,
            expected_outcome=None,
            potential_implementation=None,
        )

        _manager.save_entry(entry)
        log.info(
            "error_pattern_captain_log_saved",
            fingerprint=event.fingerprint,
            component=event.component,
            event_name=event.event_name,
            occurrences=event.occurrences,
            scope=scope.value,
        )

    return handler


# ---------------------------------------------------------------------------
# Wave 3 — ADR-0059 (Context Quality Stream)
# ---------------------------------------------------------------------------


def build_compaction_quality_captain_log_handler(manager: Any | None = None) -> Any:
    """Build handler that writes Captain's Log entries on context-quality incidents.

    Subscribes ``cg:captain-log`` to ``stream:context.compaction_quality_poor``.
    Each ``CompactionQualityIncidentEvent`` becomes a ``CONFIG_PROPOSAL`` entry
    with ``category=KNOWLEDGE_QUALITY`` and ``scope=ORCHESTRATOR``.

    Dedup and suppression are handled by the existing ``CaptainLogManager``
    infrastructure (ADR-0030): passing the same fingerprint increments
    ``seen_count`` rather than creating a duplicate entry.  ADR-0030 dedup
    also merges any overlap with the ADR-0056 cluster path that fires on
    the same ``compaction_quality.poor`` warning.

    Args:
        manager: Optional ``CaptainLogManager`` instance.  When ``None``, a
            fresh instance is created lazily inside the handler.

    Returns:
        Async handler for ``cg:captain-log`` on
        ``stream:context.compaction_quality_poor``.
    """

    async def handler(event: EventBase) -> None:
        if not isinstance(event, CompactionQualityIncidentEvent):
            return

        from datetime import datetime, timezone

        from personal_agent.captains_log.manager import CaptainLogManager
        from personal_agent.captains_log.models import (
            CaptainLogEntry,
            CaptainLogEntryType,
            ChangeCategory,
            Metric,
            ProposedChange,
            TelemetryRef,
        )

        _manager = manager or CaptainLogManager()
        now = datetime.now(timezone.utc)

        entry = CaptainLogEntry(
            entry_id=f"CL-{now.strftime('%Y%m%d-%H%M%S')}-cq-{event.fingerprint[:6]}",
            type=CaptainLogEntryType.CONFIG_PROPOSAL,
            title=(
                f'Compaction dropped "{event.dropped_entity}", user then '
                f'asked about "{event.noun_phrase}"'
            ),
            rationale=(
                f'Stage 7 dropped entity "{event.dropped_entity}" '
                f"(tier: {event.tier_affected}, {event.tokens_removed} tokens) "
                f"earlier in this session; the user then asked about "
                f'"{event.noun_phrase}" with cue "{event.recall_cue}". '
                f"The recall controller's substring match identified the "
                f"overlap (ADR-0047 D3, ADR-0059)."
            ),
            proposed_change=ProposedChange(
                what="Investigate Stage 7 trim ordering for entity priority",
                why=(
                    "Sustained context-quality incidents indicate the budget "
                    "stage is dropping entities that the user actively "
                    "references."
                ),
                how=(
                    "1) Inspect the Captain's Log entry's trace_id in Kibana "
                    "for the full compaction record.\n"
                    "2) Decide whether the trim ordering needs entity-priority "
                    "logic, or whether the budget ceiling itself is too tight.\n"
                    "3) If patterns concentrate on a single entity class, "
                    "consider promoting the entity in memory-recall scoring."
                ),
                category=ChangeCategory.KNOWLEDGE_QUALITY,
                scope=ChangeScope.ORCHESTRATOR,
                fingerprint=event.fingerprint,
                first_seen=None,
            ),
            supporting_metrics=[
                f"tokens_removed: {event.tokens_removed}",
                f"tier_affected: {event.tier_affected}",
                f"detected_at: {event.detected_at.isoformat()}",
            ],
            metrics_structured=[
                Metric(
                    name="tokens_removed",
                    value=event.tokens_removed,
                    unit="tokens",
                ),
            ],
            telemetry_refs=(
                [TelemetryRef(trace_id=event.trace_id, metric_name=None, value=None)]
                if event.trace_id
                else []
            ),
            impact_assessment=None,
            reviewer_notes=None,
            linear_issue_id=None,
            experiment_design=None,
            expected_outcome=None,
            potential_implementation=None,
        )

        _manager.save_entry(entry)
        log.info(
            "compaction_quality_captain_log_saved",
            fingerprint=event.fingerprint,
            session_id=event.session_id,
            noun_phrase=event.noun_phrase,
            dropped_entity=event.dropped_entity,
            tier_affected=event.tier_affected,
        )

    return handler


# ---------------------------------------------------------------------------
# Wave 3 — ADR-0060 (Knowledge Graph Quality Stream)
# ---------------------------------------------------------------------------


def build_graph_quality_captain_log_handler(manager: Any | None = None) -> Any:
    """Build handler that writes Captain's Log entries on graph-quality signals.

    Subscribes ``cg:graph-monitor`` to both ``stream:graph.quality_anomaly``
    (Stream 8 — daily anomaly scan) and ``stream:memory.staleness_reviewed``
    (Stream 6 — weekly freshness review).

    - ``GraphQualityAnomalyEvent``: one ``CONFIG_PROPOSAL`` per anomaly;
      severity ``"high"`` → ``category=RELIABILITY``; else ``KNOWLEDGE_QUALITY``.
      Phase 2 (flag-gated): also publishes ``ModeAdvisoryEvent`` for high-severity
      anomalies when ``graph_quality_governance_enabled=True``.

    - ``MemoryStalenessReviewedEvent``: trend-summary ``CONFIG_PROPOSAL`` only when
      ``entities_dormant ≥ settings.freshness_dormant_entity_proposal_threshold``.

    Dedup and suppression are handled by ``CaptainLogManager`` (ADR-0030).

    Args:
        manager: Optional ``CaptainLogManager`` instance.  When ``None``, a
            fresh instance is created lazily inside the handler.

    Returns:
        Async handler for ``cg:graph-monitor`` on both graph-quality streams.
    """

    async def handler(event: EventBase) -> None:
        if isinstance(event, GraphQualityAnomalyEvent):
            await _handle_graph_quality_anomaly(event, manager)
        elif isinstance(event, MemoryStalenessReviewedEvent):
            await _handle_staleness_reviewed(event, manager)

    return handler


async def _handle_graph_quality_anomaly(
    event: GraphQualityAnomalyEvent,
    manager: Any | None,
) -> None:
    """Write a Captain's Log entry for one graph-quality anomaly (ADR-0060 §D6)."""
    from datetime import datetime, timezone

    from personal_agent.captains_log.manager import CaptainLogManager
    from personal_agent.captains_log.models import (
        CaptainLogEntry,
        CaptainLogEntryType,
        ChangeCategory,
        Metric,
        ProposedChange,
        TelemetryRef,
    )
    from personal_agent.config.settings import get_settings

    _manager = manager or CaptainLogManager()
    cfg = get_settings()
    now = datetime.now(timezone.utc)
    category = (
        ChangeCategory.RELIABILITY
        if event.severity == "high"
        else ChangeCategory.KNOWLEDGE_QUALITY
    )

    entry = CaptainLogEntry(
        entry_id=f"CL-{now.strftime('%Y%m%d-%H%M%S')}-gq-{event.fingerprint[:6]}",
        type=CaptainLogEntryType.CONFIG_PROPOSAL,
        title=f"[Graph quality] {event.anomaly_type}: {event.message}",
        rationale=(
            f'Consolidation quality monitor detected anomaly type "{event.anomaly_type}" '
            f"(severity: {event.severity}). Observed value {event.observed_value:.4f}; "
            f"expected range {event.expected_range}."
        ),
        proposed_change=ProposedChange(
            what=f"Investigate {event.anomaly_type} anomaly in knowledge graph",
            why=(
                f'Daily anomaly scan ({event.observation_date}) found "{event.message}". '
                f"Observed: {event.observed_value:.4f}. Range: {event.expected_range}."
            ),
            how=(
                "1) Check the telemetry/graph_quality/GQ-*.jsonl entry for this fingerprint.\n"
                "2) Run the quality monitor interactively via brainstem diagnostics to inspect "
                "raw metrics.\n"
                "3) For extraction failures, check the ES agent-logs-* index for "
                "entity_extraction_failed events around the observation date.\n"
                "4) For structural anomalies (no_relationships_created), inspect Neo4j directly."
            ),
            category=category,
            scope=ChangeScope.SECOND_BRAIN,
            fingerprint=event.fingerprint,
            first_seen=None,
        ),
        supporting_metrics=[
            f"anomaly_type: {event.anomaly_type}",
            f"severity: {event.severity}",
            f"observed_value: {event.observed_value:.4f}",
            f"observation_date: {event.observation_date}",
        ],
        metrics_structured=[
            Metric(name="observed_value", value=event.observed_value, unit=None),
        ],
        telemetry_refs=[TelemetryRef(trace_id=event.trace_id, metric_name=None, value=None)],
        impact_assessment=None,
        reviewer_notes=None,
        linear_issue_id=None,
        experiment_design=None,
        expected_outcome=None,
        potential_implementation=None,
    )

    _manager.save_entry(entry)
    log.info(
        "graph_quality_anomaly_captain_log_saved",
        fingerprint=event.fingerprint,
        anomaly_type=event.anomaly_type,
        severity=event.severity,
        observation_date=event.observation_date,
    )

    # Phase 2 governance: high-severity → ModeAdvisoryEvent (flag-gated, default off)
    if event.severity == "high" and cfg.graph_quality_governance_enabled:
        await _publish_mode_advisory(event)


async def _publish_mode_advisory(event: GraphQualityAnomalyEvent) -> None:
    """Publish a mode advisory for high-severity graph-quality anomalies (ADR-0060 §D7)."""
    from personal_agent.events.bus import get_event_bus
    from personal_agent.events.models import STREAM_MODE_TRANSITION, ModeAdvisoryEvent

    try:
        advisory = ModeAdvisoryEvent(
            target_mode="degraded",
            surface_tag="consolidation",
            reason=f"graph_quality_anomaly:{event.anomaly_type}",
            source_component="events.pipeline_handlers",
        )
        await get_event_bus().publish(STREAM_MODE_TRANSITION, advisory)
        log.info(
            "mode_advisory_published",
            target_mode="degraded",
            surface_tag="consolidation",
            reason=advisory.reason,
            anomaly_fingerprint=event.fingerprint,
        )
    except Exception as exc:
        log.warning(
            "mode_advisory_publish_failed",
            anomaly_type=event.anomaly_type,
            error=str(exc),
        )


async def _handle_staleness_reviewed(
    event: MemoryStalenessReviewedEvent,
    manager: Any | None,
) -> None:
    """Write a trend-summary Captain's Log entry for a freshness review (ADR-0060 §D6)."""
    from datetime import datetime, timezone

    from personal_agent.captains_log.manager import CaptainLogManager
    from personal_agent.captains_log.models import (
        CaptainLogEntry,
        CaptainLogEntryType,
        ChangeCategory,
        ProposedChange,
        TelemetryRef,
    )
    from personal_agent.config.settings import get_settings

    cfg = get_settings()
    threshold = cfg.freshness_dormant_entity_proposal_threshold
    if event.entities_dormant < threshold:
        log.debug(
            "staleness_review_below_threshold",
            entities_dormant=event.entities_dormant,
            threshold=threshold,
            iso_week=event.iso_week,
        )
        return

    _manager = manager or CaptainLogManager()
    now = datetime.now(timezone.utc)

    entry = CaptainLogEntry(
        entry_id=f"CL-{now.strftime('%Y%m%d-%H%M%S')}-fr-{event.fingerprint[:6]}",
        type=CaptainLogEntryType.CONFIG_PROPOSAL,
        title=f"KG freshness review {event.iso_week}: {event.entities_dormant} dormant entities",
        rationale=(
            f"Weekly freshness review ({event.iso_week}) found {event.entities_dormant} "
            f"DORMANT entities (threshold: {threshold}). "
            f"Tier breakdown — warm: {event.entities_warm}, cooling: {event.entities_cooling}, "
            f"cold: {event.entities_cold}, dormant: {event.entities_dormant}. "
            f"Dominant tier: {event.dominant_tier}."
        ),
        proposed_change=ProposedChange(
            what=(
                f"Review {event.entities_dormant} dormant entities from KG "
                f"freshness report {event.iso_week}"
            ),
            why=(
                f"Dormant entity count ({event.entities_dormant}) exceeds the "
                f"proposal threshold ({threshold}). These entities have not been "
                f"accessed in over 90 days and may be candidates for archival."
            ),
            how=(
                f"1) Check telemetry/freshness_review/FR-{event.iso_week}.jsonl for details.\n"
                "2) Run brainstem diagnostics to identify the top dormant entities by name.\n"
                "3) Decide whether to archive, retain, or force-revalidate these entities.\n"
                "4) If archival is appropriate, run the freshness backfill migration script."
            ),
            category=ChangeCategory.KNOWLEDGE_QUALITY,
            scope=ChangeScope.SECOND_BRAIN,
            fingerprint=event.fingerprint,
            first_seen=None,
        ),
        supporting_metrics=[
            f"iso_week: {event.iso_week}",
            f"entities_dormant: {event.entities_dormant}",
            f"relationships_dormant: {event.relationships_dormant}",
            f"dominant_tier: {event.dominant_tier}",
            f"never_accessed_old: {event.never_accessed_old_entity_count}",
        ],
        metrics_structured=[],
        telemetry_refs=[TelemetryRef(trace_id=event.trace_id, metric_name=None, value=None)],
        impact_assessment=None,
        reviewer_notes=None,
        linear_issue_id=None,
        experiment_design=None,
        expected_outcome=None,
        potential_implementation=None,
    )

    _manager.save_entry(entry)
    log.info(
        "staleness_review_captain_log_saved",
        fingerprint=event.fingerprint,
        iso_week=event.iso_week,
        entities_dormant=event.entities_dormant,
        dominant_tier=event.dominant_tier,
    )
