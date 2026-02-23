"""Request-scoped system metrics monitoring for homeostasis control loops.

This module implements ADR-0012: Request-Scoped Metrics Monitoring, providing
automatic background monitoring of system metrics during agent request execution.

Key Features:
- Async background polling at configurable intervals (default: 5s)
- Trace correlation (all metrics tagged with trace_id)
- Threshold monitoring for control loop triggers
- Aggregated summaries for Captain's Log enrichment
- Graceful cleanup on both success and failure paths

Architecture:
    User Request → Orchestrator starts RequestMonitor
                  ↓
                  Monitor polls metrics every 5s
                  ↓
                  Logs SYSTEM_METRICS_SNAPSHOT + trace_id
                  ↓
                  Checks thresholds → Emits control signals
                  ↓
                  ModeManager evaluates transitions
                  ↓
    Request Completes → Monitor stops, returns summary

Related:
- ADR-0012: Request-Scoped Metrics Monitoring
- ADR-0013: Enhanced System Health Tool
- ../../docs/architecture/CONTROL_LOOPS_SENSORS_v0.1.md
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from personal_agent.brainstem.sensors.sensors import poll_system_metrics
from personal_agent.config import settings
from personal_agent.telemetry import SYSTEM_METRICS_SNAPSHOT, get_logger

log = get_logger(__name__)


class RequestMonitor:
    """Background system metrics monitor scoped to a specific request.

    Collects metrics at regular intervals and tags them with trace_id
    for correlation with logs and Captain's Log reflections.

    Usage:
        >>> monitor = RequestMonitor(trace_id="abc-123", interval_seconds=5.0)
        >>> await monitor.start()
        >>> # ... request executes ...
        >>> summary = await monitor.stop()
        >>> print(summary['cpu_avg'])  # Average CPU during request

    Attributes:
        trace_id: Unique identifier for the request being monitored.
        interval_seconds: Polling interval (default: 5.0 seconds).
        _task: Background asyncio task for polling.
        _samples: List of collected metric snapshots.
        _start_time: Monitor start timestamp.
        _running: Flag indicating if monitoring is active.
    """

    def __init__(
        self,
        trace_id: str,
        interval_seconds: float | None = None,
        include_gpu: bool | None = None,
    ):
        """Initialize monitor for a specific request.

        Args:
            trace_id: Unique identifier for the request.
            interval_seconds: Polling interval. Defaults to settings.request_monitoring_interval_seconds.
            include_gpu: Whether to include GPU metrics. Defaults to settings.request_monitoring_include_gpu.
        """
        self.trace_id = trace_id
        self.interval_seconds = (
            interval_seconds
            if interval_seconds is not None
            else settings.request_monitoring_interval_seconds
        )
        self.include_gpu = (
            include_gpu if include_gpu is not None else settings.request_monitoring_include_gpu
        )
        self._task: asyncio.Task[None] | None = None
        self._samples: list[dict[str, Any]] = []
        self._start_time: float | None = None
        self._running: bool = False
        self._threshold_violations: list[str] = []

    async def start(self) -> None:
        """Start background monitoring task.

        Launches an async task that polls system metrics at the configured
        interval until stop() is called.

        Raises:
            RuntimeError: If monitor is already running.
        """
        if self._running:
            raise RuntimeError(f"RequestMonitor already running for trace_id={self.trace_id}")

        self._start_time = time.time()
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

        log.info(
            "request_monitor_started",
            trace_id=self.trace_id,
            interval_seconds=self.interval_seconds,
            include_gpu=self.include_gpu,
            component="request_monitor",
        )

    async def stop(self) -> dict[str, Any]:
        """Stop monitoring and return aggregated summary.

        Cancels the background task and computes statistics across all
        collected samples.

        Returns:
            Summary dict with:
            - duration_seconds: Total monitoring duration
            - samples_collected: Number of metric snapshots
            - cpu_avg/min/max: CPU statistics (percentage)
            - memory_avg/min/max: Memory statistics (percentage)
            - gpu_avg/min/max: GPU statistics (percentage, if available)
            - threshold_violations: List of control loop thresholds exceeded

        Raises:
            RuntimeError: If monitor was not started.
        """
        if not self._running:
            raise RuntimeError(f"RequestMonitor not running for trace_id={self.trace_id}")

        self._running = False

        # Cancel background task
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass  # Expected

        # Compute summary
        summary = self._compute_summary()

        log.info(
            "request_monitor_stopped",
            trace_id=self.trace_id,
            duration_seconds=summary["duration_seconds"],
            samples_collected=summary["samples_collected"],
            cpu_avg=summary.get("cpu_avg"),
            memory_avg=summary.get("memory_avg"),
            gpu_avg=summary.get("gpu_avg"),
            threshold_violations_count=len(summary["threshold_violations"]),
            component="request_monitor",
        )

        return summary

    async def _monitor_loop(self) -> None:
        """Background loop that polls metrics at interval.

        Runs until _running is False or task is cancelled.
        Logs SYSTEM_METRICS_SNAPSHOT events with trace_id.
        """
        try:
            while self._running:
                metrics = await asyncio.to_thread(poll_system_metrics)

                # Tag with trace_id and timestamp
                metrics["trace_id"] = self.trace_id
                metrics["timestamp"] = datetime.now(timezone.utc).isoformat()

                # Store sample
                self._samples.append(metrics)

                log.info(
                    SYSTEM_METRICS_SNAPSHOT,
                    trace_id=self.trace_id,
                    cpu_load=metrics.get("perf_system_cpu_load"),
                    memory_used=metrics.get("perf_system_mem_used"),
                    gpu_load=metrics.get("perf_system_gpu_load"),
                    component="request_monitor",
                )

                # Check thresholds for control loops
                violations = self._check_thresholds(metrics)
                if violations:
                    self._threshold_violations.extend(violations)
                    log.warning(
                        "metrics_threshold_violated",
                        trace_id=self.trace_id,
                        violations=violations,
                        component="request_monitor",
                    )

                # Wait for next interval
                await asyncio.sleep(self.interval_seconds)

        except asyncio.CancelledError:
            # Normal shutdown
            log.debug(
                "monitor_loop_cancelled",
                trace_id=self.trace_id,
                samples_collected=len(self._samples),
                component="request_monitor",
            )
            raise
        except Exception as e:
            # Unexpected error - log but don't crash request
            log.error(
                "monitor_loop_error",
                trace_id=self.trace_id,
                error_type=type(e).__name__,
                error_message=str(e),
                component="request_monitor",
            )

    def _check_thresholds(self, metrics: dict[str, Any]) -> list[str]:
        """Check metrics against control loop thresholds.

        Compares current metrics against thresholds defined in modes.yaml
        to determine if mode transitions should be triggered.

        Args:
            metrics: Current system metrics snapshot.

        Returns:
            List of threshold violation descriptions.

        Note:
            Thresholds from modes.yaml (simplified):
            - NORMAL → ALERT: CPU > 85% or Memory > 90%
            - ALERT → DEGRADED: CPU > 95% or Memory > 95%
        """
        violations = []

        # Extract metric values (use flat keys from poll_system_metrics)
        cpu_percent = metrics.get("perf_system_cpu_load")
        memory_percent = metrics.get("perf_system_mem_used")

        if cpu_percent is not None:
            if cpu_percent > 95:
                violations.append(f"CPU critically high: {cpu_percent:.1f}% (DEGRADED threshold)")
            elif cpu_percent > 85:
                violations.append(f"CPU high: {cpu_percent:.1f}% (ALERT threshold)")

        if memory_percent is not None:
            if memory_percent > 95:
                violations.append(
                    f"Memory critically high: {memory_percent:.1f}% (DEGRADED threshold)"
                )
            elif memory_percent > 90:
                violations.append(f"Memory high: {memory_percent:.1f}% (ALERT threshold)")

        return violations

    def _compute_summary(self) -> dict[str, Any]:
        """Compute aggregated statistics across all samples.

        Returns:
            Summary dict with min/max/avg for CPU, memory, GPU.
        """
        if not self._samples:
            return {
                "duration_seconds": 0.0,
                "samples_collected": 0,
                "threshold_violations": list(set(self._threshold_violations)),
            }

        duration = time.time() - (self._start_time or time.time())

        # Extract values (use flat keys from poll_system_metrics)
        cpu_values = [
            s.get("perf_system_cpu_load")
            for s in self._samples
            if s.get("perf_system_cpu_load") is not None
        ]
        memory_values = [
            s.get("perf_system_mem_used")
            for s in self._samples
            if s.get("perf_system_mem_used") is not None
        ]
        gpu_values = [
            s.get("perf_system_gpu_load")
            for s in self._samples
            if s.get("perf_system_gpu_load") is not None
        ]

        summary: dict[str, Any] = {
            "duration_seconds": round(duration, 2),
            "samples_collected": len(self._samples),
            "threshold_violations": list(set(self._threshold_violations)),
        }

        # CPU stats
        if cpu_values:
            summary["cpu_min"] = round(min(cpu_values), 1)
            summary["cpu_max"] = round(max(cpu_values), 1)
            summary["cpu_avg"] = round(sum(cpu_values) / len(cpu_values), 1)

        # Memory stats
        if memory_values:
            summary["memory_min"] = round(min(memory_values), 1)
            summary["memory_max"] = round(max(memory_values), 1)
            summary["memory_avg"] = round(sum(memory_values) / len(memory_values), 1)

        # GPU stats (if available)
        if gpu_values:
            summary["gpu_min"] = round(min(gpu_values), 1)
            summary["gpu_max"] = round(max(gpu_values), 1)
            summary["gpu_avg"] = round(sum(gpu_values) / len(gpu_values), 1)

        return summary
