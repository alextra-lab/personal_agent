"""Consumer handlers for Phase 3 pipeline decoupling (FRE-159 / ADR-0041 Phase 3).

Each builder returns an async handler suitable for ``EventBus.subscribe()``.
Handlers are pure functions over the event bus — they import heavy dependencies
lazily so the module loads cheaply and stays testable with lightweight mocks.
"""

from __future__ import annotations

from typing import Any

from personal_agent.captains_log.models import ChangeScope
from personal_agent.events.models import (
    ConsolidationCompletedEvent,
    ErrorPatternDetectedEvent,
    EventBase,
    FeedbackReceivedEvent,
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
