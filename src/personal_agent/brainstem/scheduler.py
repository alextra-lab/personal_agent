"""Brainstem scheduler for adaptive second brain consolidation (Phase 2.2).

This module monitors system resources and triggers second brain consolidation
when conditions are met (idle time, low resource usage).
"""

import asyncio
from datetime import datetime, timezone

from personal_agent.brainstem.sensors.sensors import poll_system_metrics
from personal_agent.config.settings import get_settings
from personal_agent.second_brain.consolidator import SecondBrainConsolidator
from personal_agent.telemetry import get_logger

log = get_logger(__name__)
settings = get_settings()


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

    def __init__(self) -> None:  # noqa: D107
        """Initialize scheduler with consolidation thresholds."""
        self.running = False
        self.consolidator: SecondBrainConsolidator | None = None
        self.last_consolidation: datetime | None = None
        self.last_request_time: datetime | None = None

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

    async def start(self) -> None:
        """Start the scheduler background task."""
        if self.running:
            log.warning("scheduler_already_running")
            return

        self.running = True
        log.info("brainstem_scheduler_started")

        # Start background monitoring loop
        asyncio.create_task(self._monitoring_loop())

    async def stop(self) -> None:
        """Stop the scheduler."""
        self.running = False
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

        # Check system resources
        try:
            metrics = poll_system_metrics()
            cpu_load = metrics.get("perf_system_cpu_load", 0.0)
            memory_used = metrics.get("perf_system_mem_used", 0.0)

            if cpu_load > self.cpu_threshold:
                log.debug(
                    "consolidation_skipped_cpu_high",
                    cpu_load=cpu_load,
                    threshold=self.cpu_threshold,
                )
                return False

            if memory_used > self.memory_threshold:
                log.debug(
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
