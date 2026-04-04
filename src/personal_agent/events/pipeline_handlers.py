"""Consumer handlers for Phase 3 pipeline decoupling (FRE-159 / ADR-0041 Phase 3).

Each builder returns an async handler suitable for ``EventBus.subscribe()``.
Handlers are pure functions over the event bus — they import heavy dependencies
lazily so the module loads cheaply and stays testable with lightweight mocks.
"""

from __future__ import annotations

from typing import Any

from personal_agent.events.models import (
    ConsolidationCompletedEvent,
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


def build_consolidation_insights_handler() -> Any:
    """Build handler that runs insights analysis on ``consolidation.completed``.

    Skips analysis when no captures were processed (nothing new to analyse).

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
        from personal_agent.insights.engine import InsightsEngine
        from personal_agent.telemetry.queries import TelemetryQueries

        shared_es = _default_elasticsearch_async_client()
        queries = TelemetryQueries(es_client=shared_es)
        engine = InsightsEngine(telemetry_queries=queries)
        try:
            insights = await engine.analyze_patterns(days=7)
            log.info(
                "insights_analysis_from_consolidation",
                event_id=event.event_id,
                captures_processed=event.captures_processed,
                insights_count=len(insights),
            )
        finally:
            await queries.disconnect()

    return handler


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


def build_freshness_handler() -> Any:
    """Build no-op handler for memory access tracking (Phase 4 stub).

    Acknowledges ``memory.accessed`` and ``memory.entities_updated`` events
    immediately. This is a placeholder for the follow-on ADR that designs
    the actual knowledge graph freshness consumer.

    Returns:
        Async handler for ``cg:freshness`` on ``stream:memory.*`` events.
    """

    async def handler(event: EventBase) -> None:
        # Phase 4 stub: just acknowledge receipt
        log.debug(
            "freshness_event_acknowledged",
            event_id=event.event_id,
            event_type=event.event_type,
        )

    return handler
