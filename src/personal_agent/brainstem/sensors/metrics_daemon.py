"""Service-lifetime metrics polling daemon.

This module implements ADR-0021 by moving system metrics polling from
request scope to a single service-lifetime background task.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from personal_agent.brainstem.sensors.sensors import poll_system_metrics
from personal_agent.telemetry import SENSOR_POLL, get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class MetricsSample:
    """Single metrics sample captured by the daemon.

    Attributes:
        timestamp: UNIX timestamp when the sample was captured.
        metrics: Raw metrics payload from `poll_system_metrics`.
    """

    timestamp: float
    metrics: dict[str, Any]


class MetricsDaemon:
    """Continuously polls system metrics for the service lifetime.

    The daemon keeps a ring buffer of recent metrics and provides non-blocking
    readers for latest/window queries used by request monitoring and scheduler.
    """

    def __init__(
        self,
        poll_interval_seconds: float = 5.0,
        es_emit_interval_seconds: float = 30.0,
        buffer_size: int = 720,
    ) -> None:
        """Initialize daemon configuration and in-memory state.

        Args:
            poll_interval_seconds: Poll cadence in seconds.
            es_emit_interval_seconds: Telemetry emission cadence for `SENSOR_POLL`.
            buffer_size: Maximum number of samples to retain in ring buffer.
        """
        self._poll_interval_seconds = poll_interval_seconds
        self._es_emit_interval_seconds = es_emit_interval_seconds
        self._buffer: deque[MetricsSample] = deque(maxlen=buffer_size)
        self._latest: MetricsSample | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._polls_since_emit = 0
        self._emit_every_n_polls = max(
            1, math.ceil(self._es_emit_interval_seconds / self._poll_interval_seconds)
        )

    async def start(self) -> None:
        """Start background polling if not already running."""
        if self._running:
            log.warning("metrics_daemon_already_running")
            return

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        log.info(
            "metrics_daemon_started",
            poll_interval_seconds=self._poll_interval_seconds,
            es_emit_interval_seconds=self._es_emit_interval_seconds,
            buffer_size=self._buffer.maxlen,
        )

    async def stop(self) -> None:
        """Stop background polling and wait for task shutdown."""
        if not self._running:
            return

        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        log.info("metrics_daemon_stopped")

    def get_latest(self) -> MetricsSample | None:
        """Return the most recent sample without blocking."""
        return self._latest

    def get_window(self, seconds: float) -> list[MetricsSample]:
        """Return samples within the last `seconds`.

        Args:
            seconds: Size of lookback window in seconds.

        Returns:
            Samples from the ring buffer newer than the cutoff.
        """
        cutoff = time.time() - max(0.0, seconds)
        return [sample for sample in self._buffer if sample.timestamp >= cutoff]

    async def _poll_loop(self) -> None:
        """Run polling loop until daemon is stopped."""
        try:
            while self._running:
                try:
                    raw = await asyncio.to_thread(poll_system_metrics)
                    sample = MetricsSample(timestamp=time.time(), metrics=raw)
                    self._latest = sample
                    self._buffer.append(sample)
                    self._polls_since_emit += 1

                    if self._polls_since_emit >= self._emit_every_n_polls:
                        log.info(
                            SENSOR_POLL,
                            cpu_load=raw.get("perf_system_cpu_load"),
                            memory_used=raw.get("perf_system_mem_used"),
                            gpu_load=raw.get("perf_system_gpu_load"),
                            disk_usage=raw.get("perf_system_disk_usage_percent"),
                            component="metrics_daemon",
                        )
                        self._polls_since_emit = 0
                except Exception as e:
                    log.error(
                        "metrics_daemon_poll_error",
                        error=str(e),
                        error_type=type(e).__name__,
                    )

                await asyncio.sleep(self._poll_interval_seconds)
        except asyncio.CancelledError:
            log.debug("metrics_daemon_poll_loop_cancelled")
            raise


_global_metrics_daemon: MetricsDaemon | None = None


def set_global_metrics_daemon(daemon: MetricsDaemon | None) -> None:
    """Set process-global daemon reference for non-FastAPI contexts."""
    global _global_metrics_daemon
    _global_metrics_daemon = daemon


def get_global_metrics_daemon() -> MetricsDaemon | None:
    """Get process-global daemon reference if initialized."""
    return _global_metrics_daemon
