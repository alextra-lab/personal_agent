"""Tests for daemon-backed RequestMonitor."""

import time

import pytest

from personal_agent.brainstem.sensors.metrics_daemon import MetricsSample
from personal_agent.brainstem.sensors.request_monitor import RequestMonitor


class FakeDaemon:
    """Simple test double for daemon window reads."""

    def __init__(self, samples: list[MetricsSample]) -> None:
        self._samples = samples
        self.last_window_seconds: float | None = None

    def get_window(self, seconds: float) -> list[MetricsSample]:
        self.last_window_seconds = seconds
        return self._samples


@pytest.mark.asyncio
async def test_request_monitor_stop_computes_summary_from_daemon_window() -> None:
    """RequestMonitor.stop should aggregate metrics from daemon samples."""
    now = time.time()
    daemon = FakeDaemon(
        samples=[
            MetricsSample(
                timestamp=now - 2,
                metrics={
                    "perf_system_cpu_load": 10.0,
                    "perf_system_mem_used": 40.0,
                    "perf_system_gpu_load": 1.0,
                },
            ),
            MetricsSample(
                timestamp=now - 1,
                metrics={
                    "perf_system_cpu_load": 30.0,
                    "perf_system_mem_used": 60.0,
                    "perf_system_gpu_load": 5.0,
                },
            ),
        ]
    )
    monitor = RequestMonitor(trace_id="trace-1", daemon=daemon)

    await monitor.start()
    summary = await monitor.stop()

    assert summary["samples_collected"] == 2
    assert summary["cpu_min"] == 10.0
    assert summary["cpu_max"] == 30.0
    assert summary["cpu_avg"] == 20.0
    assert summary["memory_avg"] == 50.0
    assert summary["gpu_avg"] == 3.0
    assert daemon.last_window_seconds is not None
    assert daemon.last_window_seconds >= 0


@pytest.mark.asyncio
async def test_request_monitor_detects_threshold_violations_from_samples() -> None:
    """RequestMonitor should include threshold violations in summary."""
    daemon = FakeDaemon(
        samples=[
            MetricsSample(
                timestamp=time.time(),
                metrics={
                    "perf_system_cpu_load": 96.0,
                    "perf_system_mem_used": 92.0,
                },
            )
        ]
    )
    monitor = RequestMonitor(trace_id="trace-2", daemon=daemon)

    await monitor.start()
    summary = await monitor.stop()

    violations = summary["threshold_violations"]
    assert len(violations) >= 2
    assert any("CPU critically high" in item for item in violations)
    assert any("Memory high" in item for item in violations)


@pytest.mark.asyncio
async def test_request_monitor_stop_without_start_raises() -> None:
    """stop() should reject calls before start()."""
    monitor = RequestMonitor(trace_id="trace-3", daemon=FakeDaemon(samples=[]))

    with pytest.raises(RuntimeError, match="not running"):
        await monitor.stop()
