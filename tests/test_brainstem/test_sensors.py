"""Tests for sensor polling."""

import pytest

from personal_agent.brainstem.sensors import get_system_metrics_snapshot, poll_system_metrics
from personal_agent.brainstem.sensors.platforms.apple import (
    is_apple_silicon,
    poll_apple_gpu_metrics,
)


def test_poll_system_metrics() -> None:
    """Test poll_system_metrics returns metrics dictionary."""
    metrics = poll_system_metrics()

    # Should return a dict (may be empty if psutil not available)
    assert isinstance(metrics, dict)

    # If psutil is available, should have some metrics
    if metrics:
        # Check for expected metric keys
        assert "perf_system_cpu_load" in metrics or "perf_system_mem_used" in metrics


def test_poll_system_metrics_types() -> None:
    """Test poll_system_metrics returns correct types."""
    metrics = poll_system_metrics()

    if "perf_system_cpu_load" in metrics:
        assert isinstance(metrics["perf_system_cpu_load"], (int, float))
        assert 0 <= metrics["perf_system_cpu_load"] <= 100

    if "perf_system_mem_used" in metrics:
        assert isinstance(metrics["perf_system_mem_used"], (int, float))
        assert 0 <= metrics["perf_system_mem_used"] <= 100

    if "perf_system_disk_used" in metrics:
        assert isinstance(metrics["perf_system_disk_used"], (int, float))
        assert 0 <= metrics["perf_system_disk_used"] <= 100


def test_get_system_metrics_snapshot() -> None:
    """Test get_system_metrics_snapshot returns comprehensive metrics."""
    metrics = get_system_metrics_snapshot()

    # Should return a dict (may be empty if psutil not available)
    assert isinstance(metrics, dict)

    # If psutil is available, should have more detailed metrics
    if metrics:
        # Should have at least CPU load
        assert "perf_system_cpu_load" in metrics or "perf_system_mem_used" in metrics


def test_get_system_metrics_snapshot_detailed() -> None:
    """Test get_system_metrics_snapshot includes detailed metrics."""
    metrics = get_system_metrics_snapshot()

    if metrics:
        # Check for detailed metrics if available
        if "perf_system_cpu_count" in metrics:
            assert isinstance(metrics["perf_system_cpu_count"], int)
            assert metrics["perf_system_cpu_count"] > 0

        if "perf_system_mem_total_gb" in metrics:
            assert isinstance(metrics["perf_system_mem_total_gb"], (int, float))
            assert metrics["perf_system_mem_total_gb"] > 0


def test_sensors_handle_errors_gracefully() -> None:
    """Test sensors handle errors without crashing."""
    # These should not raise exceptions even if psutil fails
    try:
        metrics1 = poll_system_metrics()
        assert isinstance(metrics1, dict)
    except Exception:
        pytest.fail("poll_system_metrics should not raise exceptions")

    try:
        metrics2 = get_system_metrics_snapshot()
        assert isinstance(metrics2, dict)
    except Exception:
        pytest.fail("get_system_metrics_snapshot should not raise exceptions")


def test_is_apple_silicon() -> None:
    """Test Apple Silicon detection."""
    result = is_apple_silicon()
    assert isinstance(result, bool)
    # Result depends on actual platform, so we just verify it returns a bool


def test_poll_apple_gpu_metrics() -> None:
    """Test Apple GPU metrics polling."""
    metrics = poll_apple_gpu_metrics()

    # Should always return a dict (may be empty if not Apple Silicon or powermetrics unavailable)
    assert isinstance(metrics, dict)

    # If metrics are returned, verify structure
    if metrics:
        # Should have expected keys if GPU data was found
        if "perf_system_gpu_load" in metrics:
            assert isinstance(metrics["perf_system_gpu_load"], (int, float))
            assert 0 <= metrics["perf_system_gpu_load"] <= 100

        if "perf_system_gpu_power_w" in metrics:
            assert isinstance(metrics["perf_system_gpu_power_w"], (int, float))
            assert metrics["perf_system_gpu_power_w"] >= 0

        if "perf_system_gpu_temp_c" in metrics:
            assert isinstance(metrics["perf_system_gpu_temp_c"], (int, float))
            # Temperature should be reasonable (0-150C for GPU)
            assert -50 <= metrics["perf_system_gpu_temp_c"] <= 150


def test_poll_system_metrics_includes_gpu() -> None:
    """Test that poll_system_metrics includes GPU metrics when available."""
    metrics = poll_system_metrics()

    # Should have basic metrics
    assert isinstance(metrics, dict)

    # GPU metrics may or may not be present depending on platform and permissions
    # Just verify the function doesn't crash and returns a dict
    if "perf_system_gpu_load" in metrics:
        assert isinstance(metrics["perf_system_gpu_load"], (int, float))


def test_get_system_metrics_snapshot_includes_gpu() -> None:
    """Test that get_system_metrics_snapshot includes GPU metrics when available."""
    metrics = get_system_metrics_snapshot()

    # Should have basic metrics
    assert isinstance(metrics, dict)

    # GPU metrics may or may not be present depending on platform and permissions
    if "perf_system_gpu_load" in metrics:
        assert isinstance(metrics["perf_system_gpu_load"], (int, float))
