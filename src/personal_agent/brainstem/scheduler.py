"""Brainstem scheduler for adaptive second brain consolidation (Phase 2.2).

This module monitors system resources and triggers second brain consolidation
when conditions are met (idle time, low resource usage). Also runs data
lifecycle tasks (Phase 2.3): hourly disk check, daily archive, weekly purge.
"""

import asyncio
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, cast

from personal_agent.brainstem.sensors.sensors import poll_system_metrics
from personal_agent.captains_log.es_indexer import schedule_es_index
from personal_agent.captains_log.manager import CaptainLogManager
from personal_agent.config.settings import get_settings
from personal_agent.insights import InsightsEngine
from personal_agent.insights.engine import INSIGHTS_INDEX_PREFIX
from personal_agent.memory.service import MemoryService
from personal_agent.second_brain.consolidator import SecondBrainConsolidator
from personal_agent.second_brain.quality_monitor import ConsolidationQualityMonitor
from personal_agent.telemetry import SENSOR_POLL, get_logger
from personal_agent.telemetry.queries import TelemetryQueries
from personal_agent.telemetry.lifecycle_manager import DataLifecycleManager

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch

log = get_logger(__name__)
settings = get_settings()

# Lifecycle schedule (Phase 2.3)
LIFECYCLE_CHECK_INTERVAL_SECONDS = 60  # Check every minute whether to run tasks
DISK_CHECK_INTERVAL_SECONDS = 3600     # Hourly disk check
ARCHIVE_HOUR_UTC = 2                   # Daily archive at 2 AM UTC
PURGE_WEEKDAY = 6                      # Sunday
PURGE_HOUR_UTC = 3                     # Weekly purge at 3 AM UTC Sunday
BACKFILL_INTERVAL_SECONDS = 600        # Captain's Log ES backfill every 10 minutes (FRE-30)


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
    ) -> None:  # noqa: D107
        """Initialize scheduler with consolidation thresholds and optional lifecycle ES client."""
        self.running = False
        self.consolidator: SecondBrainConsolidator | None = None
        self.last_consolidation: datetime | None = None
        self.last_request_time: datetime | None = None
        self._monitoring_task: asyncio.Task[None] | None = None
        self._lifecycle_task: asyncio.Task[None] | None = None

        # Data lifecycle (Phase 2.3)
        self.lifecycler = DataLifecycleManager(es_client=lifecycle_es_client)
        self._last_disk_check: datetime | None = None
        self._last_archive_date: date | None = None
        self._last_purge_week: tuple[int, int] | None = None  # (year, week)
        self._backfill_es_logger = backfill_es_logger
        self._last_backfill_run: datetime | None = None
        self.insights_engine = InsightsEngine()
        self._last_insights_daily_date: datetime | None = None
        self._last_insights_week: tuple[int, int] | None = None  # (year, week)
        self._last_quality_check_date: date | None = None
        self.quality_monitor = quality_monitor or ConsolidationQualityMonitor(
            memory_service=memory_service,
            telemetry_queries=TelemetryQueries(
                es_client=cast("AsyncElasticsearch | None", lifecycle_es_client)
            ),
        )

        # Configuration from settings
        self.idle_time_seconds = getattr(
            settings, "second_brain_idle_time_seconds", 300
        )  # 5 minutes
        self.cpu_threshold = getattr(settings, "second_brain_cpu_threshold", 50.0)  # 50%
        self.memory_threshold = getattr(settings, "second_brain_memory_threshold", 70.0)  # 70%
        self.check_interval_seconds = getattr(
            settings, "second_brain_check_interval_seconds", 60
        )  # 1 minute
        self.min_consolidation_interval_seconds = getattr(
            settings, "second_brain_min_interval_seconds", 3600
        )  # 1 hour
        self.insights_daily_run_hour_utc = getattr(settings, "insights_daily_run_hour_utc", 6)
        self.insights_weekly_day = getattr(settings, "insights_weekly_day", 6)
        self.insights_weekly_run_hour_utc = getattr(settings, "insights_weekly_run_hour_utc", 9)
        self.quality_monitor_enabled = getattr(settings, "quality_monitor_enabled", True)
        self.quality_monitor_daily_run_hour_utc = getattr(
            settings, "quality_monitor_daily_run_hour_utc", 5
        )
        self.quality_monitor_anomaly_window_days = getattr(
            settings, "quality_monitor_anomaly_window_days", 7
        )

    async def start(self) -> None:
        """Start the scheduler background task."""
        if self.running:
            log.warning("scheduler_already_running")
            return

        self.running = True
        log.info("brainstem_scheduler_started")

        # Start background monitoring loop
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        # Data lifecycle loop (hourly disk check, daily archive, weekly purge)
        self._lifecycle_task = asyncio.create_task(self._lifecycle_loop())

    async def stop(self) -> None:
        """Stop the scheduler."""
        self.running = False
        pending_tasks = [
            task
            for task in (self._monitoring_task, self._lifecycle_task)
            if task is not None and not task.done()
        ]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._monitoring_task = None
        self._lifecycle_task = None

        queries = getattr(self.quality_monitor, "_queries", None)
        if isinstance(queries, TelemetryQueries):
            await queries.disconnect()
        log.info("brainstem_scheduler_stopped")

    def record_request(self) -> None:
        """Record that a request was processed (updates last_request_time).

        Call this from the orchestrator/service when a request completes.
        """
        self.last_request_time = datetime.now(timezone.utc)

    async def _monitoring_loop(self) -> None:
        """Background monitoring loop that checks conditions and triggers consolidation."""
        while self.running:
            try:
                await asyncio.sleep(self.check_interval_seconds)

                if not settings.enable_second_brain:
                    continue

                # Check if conditions are met
                if await self._should_consolidate():
                    await self._trigger_consolidation()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(
                    "scheduler_monitoring_loop_error",
                    error=str(e),
                    exc_info=True,
                )

    async def _should_consolidate(self) -> bool:
        """Check if consolidation should be triggered.

        Returns:
            True if conditions are met for consolidation
        """
        # Check minimum interval since last consolidation
        if self.last_consolidation:
            time_since_last = (datetime.now(timezone.utc) - self.last_consolidation).total_seconds()
            if time_since_last < self.min_consolidation_interval_seconds:
                return False

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
            metrics = await asyncio.to_thread(poll_system_metrics)
            cpu_load = metrics.get("perf_system_cpu_load", 0.0)
            memory_used = metrics.get("perf_system_mem_used", 0.0)
            gpu_load = metrics.get("perf_system_gpu_load")

            # Emit sensor_poll at INFO so the ES handler can forward it
            # to the System Health dashboard.
            log.info(
                SENSOR_POLL,
                cpu_load=cpu_load,
                memory_used=memory_used,
                gpu_load=gpu_load,
                disk_usage=metrics.get("perf_system_disk_usage_percent"),
                component="scheduler",
            )

            if cpu_load > self.cpu_threshold:
                log.info(
                    "consolidation_skipped_cpu_high",
                    cpu_load=cpu_load,
                    threshold=self.cpu_threshold,
                )
                return False

            if memory_used > self.memory_threshold:
                log.info(
                    "consolidation_skipped_memory_high",
                    memory_used=memory_used,
                    threshold=self.memory_threshold,
                )
                return False

            return True

        except Exception as e:
            log.warning(
                "consolidation_check_failed",
                error=str(e),
            )
            return False

    async def _trigger_consolidation(self) -> None:
        """Trigger second brain consolidation."""
        log.info("consolidation_triggered")

        try:
            if not self.consolidator:
                self.consolidator = SecondBrainConsolidator()

            # Consolidate recent captures (last 7 days, up to 50 captures)
            result = await self.consolidator.consolidate_recent_captures(days=7, limit=50)

            self.last_consolidation = datetime.now(timezone.utc)

            log.info(
                "consolidation_completed",
                **result,
            )

        except Exception as e:
            log.error(
                "consolidation_failed",
                error=str(e),
                exc_info=True,
            )

    async def _lifecycle_loop(self) -> None:
        """Run data lifecycle tasks: hourly disk check, daily 2AM archive, weekly Sunday 3AM purge."""
        while self.running:
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
                        )

                # Daily at 2 AM UTC: archive old data
                today = now.date()
                if lifecycle_enabled and now.hour == ARCHIVE_HOUR_UTC and (
                    self._last_archive_date is None or self._last_archive_date != today
                ):
                    for data_type in ("file_logs", "captains_log_captures", "captains_log_reflections"):
                        await self.lifecycler.archive_old_data(data_type)
                    self._last_archive_date = today

                # Weekly Sunday 3 AM UTC: purge expired + ES cleanup
                year, week, _ = now.isocalendar()
                if lifecycle_enabled and (
                    now.weekday() == PURGE_WEEKDAY
                    and now.hour == PURGE_HOUR_UTC
                    and (self._last_purge_week is None or self._last_purge_week != (year, week))
                ):
                    for data_type in ("file_logs", "captains_log_captures", "captains_log_reflections"):
                        await self.lifecycler.purge_expired_data(data_type)
                    await self.lifecycler.cleanup_elasticsearch_indices()
                    self._last_purge_week = (year, week)

                # Daily insights analysis (default 6 AM UTC)
                if (
                    getattr(settings, "insights_enabled", True)
                    and now.hour == self.insights_daily_run_hour_utc
                    and (
                        self._last_insights_daily_date is None
                        or self._last_insights_daily_date.date() != today
                    )
                ):
                    insights = await self.insights_engine.analyze_patterns(days=7)
                    self._last_insights_daily_date = now
                    log.info("insights_daily_analysis_completed", insights_count=len(insights))

                # Weekly insights -> Captain's Log proposals (default Sunday 9 AM UTC)
                if (
                    getattr(settings, "insights_enabled", True)
                    and now.weekday() == self.insights_weekly_day
                    and now.hour == self.insights_weekly_run_hour_utc
                    and (
                        self._last_insights_week is None
                        or self._last_insights_week != (year, week)
                    )
                ):
                    insights = await self.insights_engine.analyze_patterns(days=7)
                    proposals = await self.insights_engine.create_captain_log_proposals(insights)
                    manager = CaptainLogManager()
                    for proposal in proposals:
                        manager.save_entry(proposal)
                    summary_doc = {
                        "timestamp": now.isoformat(),
                        "record_type": "weekly_summary",
                        "insight_type": "weekly_proposals",
                        "title": "Weekly insights proposal batch",
                        "summary": "Weekly insights converted into Captain's Log config proposals.",
                        "confidence": 1.0,
                        "actionable": True,
                        "insights_count": len(insights),
                        "proposals_created": len(proposals),
                        "analysis_window_days": 7,
                    }
                    schedule_es_index(
                        index_name=f"{INSIGHTS_INDEX_PREFIX}-{now.strftime('%Y-%m-%d')}",
                        document=summary_doc,
                    )
                    self._last_insights_week = (year, week)
                    log.info(
                        "insights_weekly_proposals_created",
                        insights_count=len(insights),
                        proposal_count=len(proposals),
                    )

                # Daily quality monitoring (FRE-32)
                if self.quality_monitor_enabled and (
                    now.hour == self.quality_monitor_daily_run_hour_utc
                    and (
                        self._last_quality_check_date is None
                        or self._last_quality_check_date != today
                    )
                ):
                    await self._run_quality_monitoring()
                    self._last_quality_check_date = today

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(
                    "lifecycle_loop_error",
                    error=str(e),
                    exc_info=True,
                )

    async def _run_quality_monitoring(self) -> None:
        """Run quality monitor checks without breaking scheduler loops."""
        try:
            await self.quality_monitor.check_entity_extraction_quality(
                days=self.quality_monitor_anomaly_window_days
            )
        except Exception as e:
            log.warning("quality_monitor_entity_check_failed", error=str(e), exc_info=True)

        try:
            await self.quality_monitor.check_graph_health()
        except Exception as e:
            log.warning("quality_monitor_graph_check_failed", error=str(e), exc_info=True)

        anomalies_count = 0
        try:
            anomalies = await self.quality_monitor.detect_anomalies(
                days=self.quality_monitor_anomaly_window_days
            )
            anomalies_count = len(anomalies)
        except Exception as e:
            log.warning("quality_monitor_anomaly_check_failed", error=str(e), exc_info=True)

        log.info(
            "quality_monitor_run_completed",
            anomalies_count=anomalies_count,
            days=self.quality_monitor_anomaly_window_days,
        )
