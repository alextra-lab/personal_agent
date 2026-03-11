"""Tests for the continuous MetricsDaemon."""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from personal_agent.brainstem.sensors.metrics_daemon import (
    MetricsDaemon,
    get_global_metrics_daemon,
    set_global_metrics_daemon,
)
from personal_agent.telemetry import SENSOR_POLL


@pytest.mark.asyncio
async def test_metrics_daemon_collects_latest_and_window() -> None:
    """Daemon should collect samples and expose latest/window reads."""
    payload: dict[str, Any] = {
        "perf_system_cpu_load": 21.0,
        "perf_system_mem_used": 41.0,
    }

    with patch(
        "personal_agent.brainstem.sensors.metrics_daemon.poll_system_metrics",
        return_value=payload,
    ):
        daemon = MetricsDaemon(
            poll_interval_seconds=0.01,
            es_emit_interval_seconds=1.0,
            buffer_size=32,
        )
        await daemon.start()
        await asyncio.sleep(0.05)
        await daemon.stop()

    latest = daemon.get_latest()
    assert latest is not None
    assert latest.metrics["perf_system_cpu_load"] == 21.0

    window = daemon.get_window(5.0)
    assert len(window) >= 1
    assert all("perf_system_mem_used" in sample.metrics for sample in window)


@pytest.mark.asyncio
async def test_metrics_daemon_emits_sensor_poll_on_configured_cadence() -> None:
    """Daemon should emit SENSOR_POLL logs on emit interval cadence."""
    payload = {
        "perf_system_cpu_load": 30.0,
        "perf_system_mem_used": 55.0,
        "perf_system_gpu_load": 4.2,
    }
    observed_events: list[str] = []

    with (
        patch(
            "personal_agent.brainstem.sensors.metrics_daemon.poll_system_metrics",
            return_value=payload,
        ),
        patch("personal_agent.brainstem.sensors.metrics_daemon.log.info") as mock_log_info,
    ):
        daemon = MetricsDaemon(
            poll_interval_seconds=0.01,
            es_emit_interval_seconds=0.01,
            buffer_size=16,
        )
        await daemon.start()
        await asyncio.sleep(0.04)
        await daemon.stop()

        for call in mock_log_info.call_args_list:
            if call.args:
                observed_events.append(str(call.args[0]))

    assert SENSOR_POLL in observed_events


def test_global_metrics_daemon_getter_setter() -> None:
    """Global daemon accessor should return value set by setter."""
    daemon = MetricsDaemon()
    set_global_metrics_daemon(daemon)
    assert get_global_metrics_daemon() is daemon
    set_global_metrics_daemon(None)
    assert get_global_metrics_daemon() is None
