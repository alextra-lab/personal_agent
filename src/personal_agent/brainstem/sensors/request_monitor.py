"""Request-scoped metrics summary reader for homeostasis control loops.

This module implements the post-ADR-0021 `RequestMonitor`, which records
request timing and computes aggregates from the service-lifetime
`MetricsDaemon` ring buffer.
"""

import time
from typing import Any, Protocol

from personal_agent.brainstem.sensors.metrics_daemon import MetricsSample
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


class MetricsWindowReader(Protocol):
    """Protocol for reading a time-window of metrics samples."""

    def get_window(self, seconds: float) -> list[MetricsSample]:
        """Return daemon samples captured within the requested window."""


class RequestMonitor:
    """Request-scoped monitor that computes summaries from daemon samples.

    Tracks request start/end time and computes aggregate metrics over the
    corresponding daemon window for Captain's Log enrichment.

    Usage:
        >>> monitor = RequestMonitor(trace_id="abc-123", daemon=daemon)
        >>> await monitor.start()
        >>> # ... request executes ...
        >>> summary = await monitor.stop()
        >>> print(summary['cpu_avg'])  # Average CPU during request

    Attributes:
        trace_id: Unique identifier for the request being monitored.
        daemon: Service-lifetime metrics daemon for reading samples.
        _start_time: Monitor start timestamp.
        _running: Flag indicating if monitoring is active.
    """

    def __init__(
        self,
        trace_id: str,
        daemon: MetricsWindowReader,
    ):
        """Initialize monitor for a specific request.

        Args:
            trace_id: Unique identifier for the request.
            daemon: Service-lifetime metrics daemon for window reads.
        """
        self.trace_id = trace_id
        self.daemon = daemon
        self._start_time: float | None = None
        self._running: bool = False
        self._threshold_violations: list[str] = []

    async def start(self) -> None:
        """Start request-scoped monitoring window.

        Raises:
            RuntimeError: If monitor is already running.
        """
        if self._running:
            raise RuntimeError(f"RequestMonitor already running for trace_id={self.trace_id}")

        self._start_time = time.time()
        self._running = True

        log.info(
            "request_monitor_started",
            trace_id=self.trace_id,
            component="request_monitor",
        )

    async def stop(self) -> dict[str, Any]:
        """Stop monitoring and return aggregated summary.

        Computes statistics across daemon samples in the request window.

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

        elapsed = time.time() - (self._start_time or time.time())
        samples = self.daemon.get_window(seconds=elapsed)
        for sample in samples:
            violations = self._check_thresholds(sample.metrics)
            if violations:
                self._threshold_violations.extend(violations)

        # Compute summary
        summary = self._compute_summary(samples)

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

    def _compute_summary(self, samples: list[MetricsSample]) -> dict[str, Any]:
        """Compute aggregated statistics across all samples.

        Returns:
            Summary dict with min/max/avg for CPU, memory, GPU.
        """
        if not samples:
            return {
                "duration_seconds": 0.0,
                "samples_collected": 0,
                "threshold_violations": list(set(self._threshold_violations)),
            }

        duration = time.time() - (self._start_time or time.time())

        # Extract values (use flat keys from poll_system_metrics)
        cpu_values = self._extract_numeric_values(samples, "perf_system_cpu_load")
        memory_values = self._extract_numeric_values(samples, "perf_system_mem_used")
        gpu_values = self._extract_numeric_values(samples, "perf_system_gpu_load")

        summary: dict[str, Any] = {
            "duration_seconds": round(duration, 2),
            "samples_collected": len(samples),
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

    @staticmethod
    def _extract_numeric_values(samples: list[MetricsSample], key: str) -> list[float]:
        """Extract numeric values for a metric key from samples."""
        values: list[float] = []
        for sample in samples:
            value = sample.metrics.get(key)
            if isinstance(value, (int, float)):
                values.append(float(value))
        return values
