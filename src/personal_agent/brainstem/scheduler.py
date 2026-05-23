"""Brainstem scheduler for adaptive second brain consolidation (Phase 2.2).

This module monitors system resources and triggers second brain consolidation
when conditions are met (idle time, low resource usage). Also runs data
lifecycle tasks (Phase 2.3): hourly disk check, daily archive, weekly purge.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, cast

from personal_agent.brainstem.sensors.sensors import poll_system_metrics
from personal_agent.captains_log.feedback import FeedbackPoller
from personal_agent.captains_log.linear_client import LinearClient
from personal_agent.config.settings import get_settings
from personal_agent.insights.skill_routing_threshold_monitor import (
    SkillRoutingThresholdMonitor,
)
from personal_agent.memory.service import MemoryService
from personal_agent.second_brain.consolidator import SecondBrainConsolidator
from personal_agent.second_brain.quality_monitor import ConsolidationQualityMonitor
from personal_agent.telemetry import SENSOR_POLL, get_logger
from personal_agent.telemetry.lifecycle_manager import DataLifecycleManager
from personal_agent.telemetry.queries import TelemetryQueries
from personal_agent.telemetry.trace import SystemTraceContext

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch

    from personal_agent.brainstem.sensors.metrics_daemon import MetricsDaemon

log = get_logger(__name__)
settings = get_settings()


def _new_scheduler_trace_id(source: str) -> str:
    """Mint a system-scoped trace id for one scheduler operation (ADR-0074 §I3).

    Each logical scheduler operation (consolidation tick, lifecycle iteration,
    quality-monitor run, feedback poll, backfill) mints its own trace id so
    structured logs and downstream bus events stay correlated within the
    operation while remaining distinguishable from organic user traffic via
    the ``system:<source>`` ``kind`` field.

    Args:
        source: Short identifier of the scheduler operation, e.g.
            ``"scheduler.consolidation"`` or ``"scheduler.lifecycle"``.

    Returns:
        Newly-minted UUID trace id.
    """
    return SystemTraceContext.new(source).trace_id


# Lifecycle schedule (Phase 2.3)
LIFECYCLE_CHECK_INTERVAL_SECONDS = 60  # Check every minute whether to run tasks
DISK_CHECK_INTERVAL_SECONDS = 3600  # Hourly disk check
ARCHIVE_HOUR_UTC = 2  # Daily archive at 2 AM UTC
PURGE_WEEKDAY = 6  # Sunday
PURGE_HOUR_UTC = 3  # Weekly purge at 3 AM UTC Sunday
BACKFILL_INTERVAL_SECONDS = 600  # Captain's Log ES backfill every 10 minutes (FRE-30)


class BrainstemScheduler:
    """Scheduler for second brain consolidation tasks.

    Monitors system state and triggers consolidation when:
    - System has been idle for configured duration
    - CPU and memory usage are below thresholds
    - No active requests

    Usage:
        scheduler = BrainstemScheduler()
        await scheduler.start()  # Runs in background
        # ... later ...
        await scheduler.stop()
    """

    def __init__(
        self,
        lifecycle_es_client: object | None = None,
        backfill_es_logger: object | None = None,
        memory_service: MemoryService | None = None,
        quality_monitor: ConsolidationQualityMonitor | None = None,
        metrics_daemon: "MetricsDaemon | None" = None,
        linear_client: LinearClient | None = None,
    ) -> None:  # noqa: D107
        """Initialize scheduler with consolidation thresholds and optional lifecycle ES client."""
        self.running = False
        self.consolidator: SecondBrainConsolidator | None = None
        self.last_consolidation: datetime | None = None
        self.last_request_time: datetime | None = None
        self._active_request_count = 0
        self._lifecycle_task: asyncio.Task[None] | None = None
        self.metrics_daemon = metrics_daemon

        # Data lifecycle (Phase 2.3)
        self.lifecycler = DataLifecycleManager(es_client=lifecycle_es_client)
        self._last_disk_check: datetime | None = None
        self._last_archive_date: date | None = None
        self._last_purge_week: tuple[int, int] | None = None  # (year, week)
        self._backfill_es_logger = backfill_es_logger
        self._last_backfill_run: datetime | None = None
        self._last_quality_check_date: date | None = None
        self._last_feedback_date: date | None = None  # ADR-0040
        self.feedback_poller: FeedbackPoller | None = (
            FeedbackPoller(linear_client) if linear_client else None
        )
        self._linear_client = linear_client
        self.memory_service: MemoryService | None = memory_service
        self._last_freshness_review_week: tuple[int, int] | None = None
        self.feedback_polling_hour_utc = settings.feedback_polling_hour_utc
        self.quality_monitor = quality_monitor or ConsolidationQualityMonitor(
            memory_service=memory_service,
            telemetry_queries=TelemetryQueries(
                es_client=cast("AsyncElasticsearch | None", lifecycle_es_client)
            ),
        )
        self._last_skill_routing_threshold_date: date | None = None
        self.skill_routing_threshold_monitor = SkillRoutingThresholdMonitor(
            queries=TelemetryQueries(
                es_client=cast("AsyncElasticsearch | None", lifecycle_es_client)
            ),
            linear_client=linear_client,
            threshold_tokens=getattr(settings, "skill_index_p95_token_threshold", 6000),
        )

        # Configuration from settings
        self.resource_gating_enabled = getattr(
            settings, "second_brain_resource_gating_enabled", True
        )
        self.idle_time_seconds = getattr(
            settings, "second_brain_idle_time_seconds", 300
        )  # 5 minutes
        self.cpu_threshold = getattr(settings, "second_brain_cpu_threshold", 50.0)  # 50%
        self.memory_threshold = getattr(settings, "second_brain_memory_threshold", 70.0)  # 70%
        self.min_consolidation_interval_seconds = getattr(
            settings, "second_brain_min_interval_seconds", 3600
        )  # 1 hour
        self.quality_monitor_enabled = getattr(settings, "quality_monitor_enabled", True)
        self.quality_monitor_daily_run_hour_utc = getattr(
            settings, "quality_monitor_daily_run_hour_utc", 5
        )
        self.quality_monitor_anomaly_window_days = getattr(
            settings, "quality_monitor_anomaly_window_days", 7
        )

    async def start(self) -> None:
        """Start the scheduler background task."""
        start_trace_id = _new_scheduler_trace_id("scheduler.lifecycle")
        if self.running:
            log.warning("scheduler_already_running", trace_id=start_trace_id)
            return

        self.running = True
        log.info("brainstem_scheduler_started", trace_id=start_trace_id)

        # Data lifecycle loop (hourly disk check, daily archive, weekly purge)
        self._lifecycle_task = asyncio.create_task(self._lifecycle_loop())

    async def stop(self) -> None:
        """Stop the scheduler."""
        stop_trace_id = _new_scheduler_trace_id("scheduler.lifecycle")
        self.running = False
        pending_tasks = [
            task for task in (self._lifecycle_task,) if task is not None and not task.done()
        ]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._lifecycle_task = None

        queries = getattr(self.quality_monitor, "_queries", None)
        if isinstance(queries, TelemetryQueries):
            await queries.disconnect()
        log.info("brainstem_scheduler_stopped", trace_id=stop_trace_id)

    @property
    def active_request_count(self) -> int:
        """Get the number of currently active service requests."""
        return self._active_request_count

    def notify_request_start(self) -> None:
        """Record that request handling has started."""
        self._active_request_count += 1

    def notify_request_end(self) -> None:
        """Record that request handling has ended.

        Decrements the active request counter and updates the last completed
        request timestamp used by idle-time consolidation checks.
        """
        self._active_request_count = max(0, self._active_request_count - 1)
        self.last_request_time = datetime.now(timezone.utc)

    def record_request(self) -> None:
        """Backward-compatible alias for request completion.

        Call this from the orchestrator/service when a request completes.
        """
        self.notify_request_end()

    async def on_request_captured(self, trace_id: str, session_id: str) -> None:
        """Event-driven handler for ``request.captured`` events (ADR-0041).

        Called by the event bus consumer instead of waiting for the polling
        loop. Converges on the same ``_should_consolidate`` /
        ``_trigger_consolidation`` path so the min-interval gate still applies.

        Args:
            trace_id: Request trace identifier.
            session_id: Session that originated the request.
        """
        if not settings.enable_second_brain:
            return

        log.info(
            "event_request_captured_received",
            trace_id=trace_id,
            session_id=session_id,
        )

        if await self._should_consolidate(trace_id=trace_id):
            await self._trigger_consolidation(trace_id=trace_id)

    async def _should_consolidate(self, *, trace_id: str | None = None) -> bool:
        """Check if consolidation should be triggered.

        Two layers of gating:

        1. **Universal guards** (always active): no in-flight requests, and at
           least ``min_consolidation_interval_seconds`` since the last run.
        2. **Host-resource guards** (deployment-conditional, behind
           ``resource_gating_enabled``): idle time, CPU load, memory pressure.

        The host-resource guards exist for the **local-inference deployment
        mode** — agent and LLM on the same machine (e.g. Apple-silicon laptop
        running MLX). There, background consolidation competes with
        user-facing inference for GPU/CPU/memory, so we defer until the host
        is quiet. Under the current **remote-inference deployment** (VPS +
        cloud or tunnelled MLX), host metrics don't correlate with inference
        load, so the gates are disabled via
        ``AGENT_SECOND_BRAIN_RESOURCE_GATING_ENABLED=false`` and only the
        universal guards apply. See ADR-0041 §Update 2026-05-14 (FRE-326).

        Args:
            trace_id: Trace identifier of the enclosing operation (ADR-0074
                §I3). Threaded from ``on_request_captured`` (request trace)
                or minted by ``_trigger_consolidation`` callers.

        Returns:
            True if conditions are met for consolidation.
        """
        if self._active_request_count > 0:
            log.debug(
                "consolidation_skipped_active_requests",
                active_request_count=self._active_request_count,
                trace_id=trace_id,
            )
            return False

        # Check minimum interval since last consolidation
        if self.last_consolidation:
            time_since_last = (datetime.now(timezone.utc) - self.last_consolidation).total_seconds()
            if time_since_last < self.min_consolidation_interval_seconds:
                return False

        if not self.resource_gating_enabled:
            return True

        # Check idle time
        if self.last_request_time:
            idle_time = (datetime.now(timezone.utc) - self.last_request_time).total_seconds()
            if idle_time < self.idle_time_seconds:
                return False
        else:
            # No requests yet - allow consolidation after initial delay
            return True

        # Check system resources and emit metrics for ES/dashboards
        try:
            if self.metrics_daemon is not None:
                latest_sample = self.metrics_daemon.get_latest()
                if latest_sample is None:
                    log.debug("consolidation_skipped_no_metrics_daemon_sample", trace_id=trace_id)
                    return False
                metrics = latest_sample.metrics
            else:
                metrics = await asyncio.to_thread(poll_system_metrics)
            cpu_load = metrics.get("perf_system_cpu_load", 0.0)
            memory_used = metrics.get("perf_system_mem_used", 0.0)
            gpu_load = metrics.get("perf_system_gpu_load")

            if self.metrics_daemon is None:
                # Emit sensor_poll here only for fallback mode. In normal mode,
                # the daemon emits SENSOR_POLL continuously.
                log.info(
                    SENSOR_POLL,
                    cpu_load=cpu_load,
                    memory_used=memory_used,
                    gpu_load=gpu_load,
                    disk_usage=metrics.get("perf_system_disk_usage_percent"),
                    component="scheduler",
                    trace_id=trace_id,
                )

            if cpu_load > self.cpu_threshold:
                log.info(
                    "consolidation_skipped_cpu_high",
                    cpu_load=cpu_load,
                    threshold=self.cpu_threshold,
                    trace_id=trace_id,
                )
                return False

            if memory_used > self.memory_threshold:
                log.info(
                    "consolidation_skipped_memory_high",
                    memory_used=memory_used,
                    threshold=self.memory_threshold,
                    trace_id=trace_id,
                )
                return False

            return True

        except Exception as e:
            log.warning(
                "consolidation_check_failed",
                error=str(e),
                trace_id=trace_id,
            )
            return False

    async def _trigger_consolidation(self, *, trace_id: str | None = None) -> None:
        """Trigger second brain consolidation and publish consolidation.completed.

        Args:
            trace_id: Trace identifier of the enclosing operation
                (ADR-0074 §I3). Threaded from ``on_request_captured`` or
                minted by ad-hoc callers.
        """
        log.info("consolidation_triggered", trace_id=trace_id)

        try:
            if not self.consolidator:
                self.consolidator = SecondBrainConsolidator()

            # Consolidate recent captures (last 7 days, up to 50 captures)
            result = await self.consolidator.consolidate_recent_captures(
                days=7,
                limit=50,
                should_pause=lambda: self._active_request_count > 0,
            )

            # Only mark a consolidation interval when real captures were found.
            # If captures_processed=0 the dir was empty (e.g. fresh container startup);
            # leaving last_consolidation=None lets the scheduler retry promptly once
            # captures arrive rather than waiting the full min_consolidation_interval.
            if result.get("captures_processed", 0) > 0:
                self.last_consolidation = datetime.now(timezone.utc)

            log.info(
                "consolidation_completed",
                **result,
                trace_id=trace_id,
            )

            # Publish consolidation.completed event (Phase 3, ADR-0041)
            await self._publish_consolidation_completed(result, trace_id=trace_id)

        except Exception as e:
            log.error(
                "consolidation_failed",
                error=str(e),
                exc_info=True,
                trace_id=trace_id,
            )

    async def _publish_consolidation_completed(
        self, result: dict[str, Any], *, trace_id: str | None = None
    ) -> None:
        """Publish ``consolidation.completed`` to trigger insights and promotion consumers.

        Args:
            result: Summary dict returned by ``consolidate_recent_captures``.
            trace_id: Trace identifier of the enclosing consolidation
                operation (ADR-0074 §I3). Threaded onto bus-publish status
                logs.
        """
        from personal_agent.events.bus import get_event_bus
        from personal_agent.events.models import (
            STREAM_CONSOLIDATION_COMPLETED,
            ConsolidationCompletedEvent,
        )

        event = ConsolidationCompletedEvent(
            captures_processed=result.get("captures_processed", 0),
            entities_created=result.get("entities_created", 0),
            entities_promoted=result.get("entities_promoted", 0),
            source_component="brainstem.scheduler",
        )
        try:
            await get_event_bus().publish(STREAM_CONSOLIDATION_COMPLETED, event)
            log.debug(
                "consolidation_completed_event_emitted",
                captures_processed=event.captures_processed,
                trace_id=trace_id,
            )
        except Exception as exc:
            log.warning(
                "consolidation_completed_event_publish_failed",
                error=str(exc),
                trace_id=trace_id,
            )

    async def _lifecycle_loop(self) -> None:
        """Run data lifecycle tasks: hourly disk check, daily 2AM archive, weekly Sunday 3AM purge."""
        while self.running:
            iteration_trace_id = _new_scheduler_trace_id("scheduler.lifecycle")
            try:
                await asyncio.sleep(LIFECYCLE_CHECK_INTERVAL_SECONDS)

                now = datetime.now(timezone.utc)
                lifecycle_enabled = getattr(settings, "data_lifecycle_enabled", True)

                # Hourly: disk check (and alert if >80%)
                if lifecycle_enabled and (
                    self._last_disk_check is None
                    or (now - self._last_disk_check).total_seconds() >= DISK_CHECK_INTERVAL_SECONDS
                ):
                    await self.lifecycler.check_disk_usage()
                    self._last_disk_check = now

                # Captain's Log ES backfill (FRE-30): periodic replay when ES available
                if self._backfill_es_logger and (
                    self._last_backfill_run is None
                    or (now - self._last_backfill_run).total_seconds() >= BACKFILL_INTERVAL_SECONDS
                ):
                    try:
                        from personal_agent.captains_log.backfill import run_backfill

                        await run_backfill(self._backfill_es_logger)
                        self._last_backfill_run = now
                    except Exception as backfill_err:
                        log.warning(
                            "captains_log_backfill_failed",
                            error=str(backfill_err),
                            exc_info=True,
                            trace_id=iteration_trace_id,
                        )

                # Daily at 2 AM UTC: archive old data
                today = now.date()
                if (
                    lifecycle_enabled
                    and now.hour == ARCHIVE_HOUR_UTC
                    and (self._last_archive_date is None or self._last_archive_date != today)
                ):
                    for data_type in (
                        "file_logs",
                        "captains_log_captures",
                        "captains_log_reflections",
                    ):
                        await self.lifecycler.archive_old_data(data_type)
                    self._last_archive_date = today

                # Weekly Sunday 3 AM UTC: purge expired + ES cleanup
                year, week, _ = now.isocalendar()
                if lifecycle_enabled and (
                    now.weekday() == PURGE_WEEKDAY
                    and now.hour == PURGE_HOUR_UTC
                    and (self._last_purge_week is None or self._last_purge_week != (year, week))
                ):
                    for data_type in (
                        "file_logs",
                        "captains_log_captures",
                        "captains_log_reflections",
                    ):
                        await self.lifecycler.purge_expired_data(data_type)
                    await self.lifecycler.cleanup_elasticsearch_indices()
                    self._last_purge_week = (year, week)

                # Weekly freshness review (FRE-166 / ADR-0042)
                if (
                    settings.freshness_enabled
                    and self.memory_service is not None
                    and (
                        self._last_freshness_review_week is None
                        or self._last_freshness_review_week != (year, week)
                    )
                ):
                    from personal_agent.brainstem.jobs.freshness_review import (
                        parse_freshness_review_schedule,
                        run_freshness_review,
                    )

                    fr_minute, fr_hour, fr_weekday = parse_freshness_review_schedule(
                        settings.freshness_review_schedule_cron
                    )
                    if (
                        now.weekday() == fr_weekday
                        and now.hour == fr_hour
                        and now.minute == fr_minute
                    ):
                        trace_fb = f"freshness-review-{year}-W{week:02d}"
                        try:
                            await run_freshness_review(self.memory_service, trace_fb)
                        except Exception as fr_err:
                            log.warning(
                                "freshness_review_failed",
                                trace_id=trace_fb,
                                error=str(fr_err),
                                exc_info=True,
                            )
                        self._last_freshness_review_week = (year, week)

                # Daily Linear feedback polling (ADR-0040 / ADR-0041 Phase 3)
                # Insights and promotion run reactively via consolidation.completed events;
                # feedback poller publishes feedback.received for cg:insights and cg:feedback.
                if (
                    self.feedback_poller is not None
                    and getattr(settings, "feedback_polling_enabled", True)
                    and now.hour == self.feedback_polling_hour_utc
                    and (self._last_feedback_date is None or self._last_feedback_date != today)
                ):
                    feedback_events: list[Any] = []
                    try:
                        feedback_events = await self.feedback_poller.check_for_feedback()
                        if feedback_events:
                            await self.feedback_poller.process_feedback(feedback_events)
                            await self._publish_feedback_events(
                                feedback_events, trace_id=iteration_trace_id
                            )
                        self._last_feedback_date = today
                        log.info(
                            "feedback_polling_completed",
                            events_count=len(feedback_events),
                            trace_id=iteration_trace_id,
                        )
                    except Exception as poll_err:
                        log.warning(
                            "feedback_polling_failed",
                            error=str(poll_err),
                            exc_info=True,
                            trace_id=iteration_trace_id,
                        )

                # Daily quality monitoring (FRE-32)
                if self.quality_monitor_enabled and (
                    now.hour == self.quality_monitor_daily_run_hour_utc
                    and (
                        self._last_quality_check_date is None
                        or self._last_quality_check_date != today
                    )
                ):
                    await self._run_quality_monitoring(trace_id=iteration_trace_id)
                    self._last_quality_check_date = today

                # Daily skill routing threshold monitor (FRE-335 / ADR-0066 D2)
                if (
                    getattr(settings, "skill_routing_threshold_monitor_enabled", True)
                    and now.hour == getattr(settings, "skill_routing_threshold_monitor_hour_utc", 5)
                    and (
                        self._last_skill_routing_threshold_date is None
                        or self._last_skill_routing_threshold_date != today
                    )
                ):
                    try:
                        await self.skill_routing_threshold_monitor.run()
                    except Exception as exc:
                        log.warning(
                            "skill_routing_threshold_monitor_failed",
                            error=str(exc),
                            exc_info=True,
                            trace_id=iteration_trace_id,
                        )
                    self._last_skill_routing_threshold_date = today

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(
                    "lifecycle_loop_error",
                    error=str(e),
                    exc_info=True,
                    trace_id=iteration_trace_id,
                )

    async def _publish_feedback_events(
        self, feedback_events: list[Any], *, trace_id: str | None = None
    ) -> None:
        """Publish ``feedback.received`` events for each processed feedback label.

        Args:
            feedback_events: List of ``FeedbackEvent`` dataclasses from the poller.
            trace_id: Trace identifier of the enclosing scheduler iteration
                (ADR-0074 §I3).
        """
        from personal_agent.captains_log.linear_client import (
            extract_issue_identifier_from_description,
        )
        from personal_agent.events.bus import get_event_bus
        from personal_agent.events.models import STREAM_FEEDBACK_RECEIVED, FeedbackReceivedEvent

        bus = get_event_bus()
        for fe in feedback_events:
            fingerprint: str | None = None
            try:
                if self._linear_client is not None:
                    issue = await self._linear_client.get_issue(fe.issue_id)
                    desc = str(issue.get("description") or "")
                    fingerprint = extract_issue_identifier_from_description(desc) or None
            except Exception:
                pass  # fingerprint remains None; event still published without it

            event = FeedbackReceivedEvent(
                issue_id=fe.issue_id,
                issue_identifier=fe.issue_identifier,
                label=fe.label,
                fingerprint=fingerprint,
                source_component="brainstem.scheduler",
            )
            try:
                await bus.publish(STREAM_FEEDBACK_RECEIVED, event)
            except Exception as exc:
                log.warning(
                    "feedback_event_publish_failed",
                    issue_identifier=fe.issue_identifier,
                    label=fe.label,
                    error=str(exc),
                    trace_id=trace_id,
                )

    async def _run_quality_monitoring(self, *, trace_id: str | None = None) -> None:
        """Run quality monitor checks without breaking scheduler loops.

        Args:
            trace_id: Trace identifier of the enclosing scheduler iteration
                (ADR-0074 §I3); when ``None`` a quality-monitor-scoped trace
                is minted so the run log line is still correlated.
        """
        trace_id = trace_id or _new_scheduler_trace_id("scheduler.quality_monitor")
        try:
            await self.quality_monitor.check_entity_extraction_quality(
                days=self.quality_monitor_anomaly_window_days, trace_id=trace_id
            )
        except Exception as e:
            log.warning(
                "quality_monitor_entity_check_failed",
                error=str(e),
                exc_info=True,
                trace_id=trace_id,
            )

        try:
            await self.quality_monitor.check_graph_health(trace_id=trace_id)
        except Exception as e:
            log.warning(
                "quality_monitor_graph_check_failed",
                error=str(e),
                exc_info=True,
                trace_id=trace_id,
            )

        anomalies_count = 0
        try:
            anomalies = await self.quality_monitor.detect_anomalies(
                days=self.quality_monitor_anomaly_window_days, trace_id=trace_id
            )
            anomalies_count = len(anomalies)
            if anomalies and settings.graph_quality_stream_enabled:
                await self._emit_graph_quality_anomalies(anomalies, trace_id=trace_id)
        except Exception as e:
            log.warning(
                "quality_monitor_anomaly_check_failed",
                error=str(e),
                exc_info=True,
                trace_id=trace_id,
            )

        log.info(
            "quality_monitor_run_completed",
            anomalies_count=anomalies_count,
            days=self.quality_monitor_anomaly_window_days,
            trace_id=trace_id,
        )

    async def _emit_graph_quality_anomalies(
        self, anomalies: list[Any], *, trace_id: str | None = None
    ) -> None:
        """Dual-write each anomaly to JSONL and publish a bus event (ADR-0060 §D8 Stream 8).

        Follows ADR-0054 D4 ordering: durable append first, bus publish second.
        Bus failures are logged and swallowed.

        Args:
            anomalies: List of ``Anomaly`` objects from ``detect_anomalies()``.
            trace_id: Trace identifier of the enclosing quality-monitor run
                (ADR-0074 §I3). Stamped onto the per-anomaly ``GraphQualityAnomaly``
                records and bus events so downstream insights consumers can
                join on the same trace.
        """
        import dataclasses
        import json
        from pathlib import Path

        from personal_agent.events.bus import get_event_bus
        from personal_agent.events.models import (
            STREAM_GRAPH_QUALITY_ANOMALY,
            GraphQualityAnomalyEvent,
        )
        from personal_agent.insights.fingerprints import pattern_fingerprint
        from personal_agent.second_brain.quality_monitor import GraphQualityAnomaly

        today = date.today().isoformat()
        output_dir = Path("telemetry/graph_quality")
        output_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = output_dir / f"GQ-{today}.jsonl"
        bus = get_event_bus()

        for anomaly in anomalies:
            fp = pattern_fingerprint("graph_quality", anomaly.anomaly_type, anomaly.message)
            gqa = GraphQualityAnomaly(
                fingerprint=fp,
                trace_id=trace_id,
                anomaly_type=anomaly.anomaly_type,
                severity=anomaly.severity,
                message=anomaly.message,
                observed_value=anomaly.observed_value,
                expected_range=anomaly.expected_range,
                metadata=anomaly.metadata,
                observation_date=today,
            )
            # Durable write first (ADR-0054 D4)
            try:
                line = json.dumps(dataclasses.asdict(gqa)) + "\n"
                with jsonl_path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            except Exception as exc:
                log.warning(
                    "graph_quality_anomaly_jsonl_failed",
                    fingerprint=fp,
                    anomaly_type=anomaly.anomaly_type,
                    error=str(exc),
                    trace_id=trace_id,
                )
                continue  # Skip bus publish if durable write failed

            # Bus publish second (ADR-0054 D4)
            try:
                event = GraphQualityAnomalyEvent(
                    fingerprint=fp,
                    anomaly_type=gqa.anomaly_type,
                    severity=gqa.severity,
                    message=gqa.message,
                    observed_value=gqa.observed_value,
                    expected_range=gqa.expected_range,
                    metadata=gqa.metadata,
                    observation_date=gqa.observation_date,
                    source_component="brainstem.scheduler",
                )
                await bus.publish(STREAM_GRAPH_QUALITY_ANOMALY, event)
                log.debug(
                    "graph_quality_anomaly_published",
                    fingerprint=fp,
                    anomaly_type=gqa.anomaly_type,
                    severity=gqa.severity,
                    trace_id=trace_id,
                )
            except Exception as exc:
                log.warning(
                    "graph_quality_anomaly_bus_failed",
                    fingerprint=fp,
                    anomaly_type=anomaly.anomaly_type,
                    error=str(exc),
                    trace_id=trace_id,
                )
