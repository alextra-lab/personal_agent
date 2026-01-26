"""Tests for sensor-level caching in poll_system_metrics().

These tests verify the caching behavior added in ADR-0014/ADR-0015 to optimize
GPU metrics polling and avoid expensive repeated I/O operations.

Tests cover:
- Cache hit/miss scenarios
- TTL expiration
- Thread safety
- Cache behavior with both poll_system_metrics() and get_system_metrics_snapshot()
"""

import threading
import time
from unittest.mock import patch

import pytest

from personal_agent.brainstem.sensors import sensors


@pytest.fixture(autouse=True)
def clear_metrics_cache():
    """Clear the metrics cache before each test."""
    # Clear cache before test
    sensors._METRICS_CACHE.clear()
    yield
    # Clear cache after test
    sensors._METRICS_CACHE.clear()


def test_poll_system_metrics_cache_miss():
    """Test that first call results in cache miss and hardware poll."""
    with (
        patch("personal_agent.brainstem.sensors.platforms.base.poll_base_metrics") as mock_base,
        patch("personal_agent.brainstem.sensors.sensors._get_platform_sensors") as mock_platform,
    ):
        # Setup mocks
        mock_base.return_value = {
            "perf_system_cpu_load": 10.5,
            "perf_system_mem_used": 50.2,
        }
        mock_platform.return_value = None  # No platform-specific sensors

        # First call should poll hardware (cache miss)
        metrics = sensors.poll_system_metrics()

        assert mock_base.call_count == 1
        assert metrics["perf_system_cpu_load"] == 10.5
        assert metrics["perf_system_mem_used"] == 50.2


def test_poll_system_metrics_cache_hit():
    """Test that second call within TTL results in cache hit."""
    with (
        patch("personal_agent.brainstem.sensors.platforms.base.poll_base_metrics") as mock_base,
        patch("personal_agent.brainstem.sensors.sensors._get_platform_sensors") as mock_platform,
    ):
        # Setup mocks
        mock_base.return_value = {
            "perf_system_cpu_load": 10.5,
            "perf_system_mem_used": 50.2,
        }
        mock_platform.return_value = None

        # First call (cache miss)
        metrics1 = sensors.poll_system_metrics()
        assert mock_base.call_count == 1

        # Second call within TTL (cache hit)
        metrics2 = sensors.poll_system_metrics()

        # Mock should still be called only once (cache hit)
        assert mock_base.call_count == 1
        assert metrics1 == metrics2


def test_poll_system_metrics_cache_expiration():
    """Test that cache expires after TTL and triggers fresh poll."""
    # Temporarily set very short TTL for testing
    original_ttl = sensors._CACHE_TTL_SECONDS
    sensors._CACHE_TTL_SECONDS = 0.1  # 100ms

    try:
        with (
            patch("personal_agent.brainstem.sensors.platforms.base.poll_base_metrics") as mock_base,
            patch(
                "personal_agent.brainstem.sensors.sensors._get_platform_sensors"
            ) as mock_platform,
        ):
            # Setup mocks
            mock_base.return_value = {
                "perf_system_cpu_load": 10.5,
                "perf_system_mem_used": 50.2,
            }
            mock_platform.return_value = None

            # First call (cache miss)
            metrics1 = sensors.poll_system_metrics()
            assert mock_base.call_count == 1

            # Wait for cache to expire
            time.sleep(0.15)

            # Second call after TTL expiration (cache miss)
            metrics2 = sensors.poll_system_metrics()

            # Mock should be called twice (cache expired)
            assert mock_base.call_count == 2
            assert metrics1 == metrics2
    finally:
        # Restore original TTL
        sensors._CACHE_TTL_SECONDS = original_ttl


def test_poll_system_metrics_cache_returns_copy():
    """Test that cache returns a copy, not reference (prevents mutation)."""
    with (
        patch("personal_agent.brainstem.sensors.platforms.base.poll_base_metrics") as mock_base,
        patch("personal_agent.brainstem.sensors.sensors._get_platform_sensors") as mock_platform,
    ):
        # Setup mocks
        mock_base.return_value = {
            "perf_system_cpu_load": 10.5,
            "perf_system_mem_used": 50.2,
        }
        mock_platform.return_value = None

        # First call
        metrics1 = sensors.poll_system_metrics()

        # Mutate returned dict
        metrics1["perf_system_cpu_load"] = 99.9

        # Second call (from cache)
        metrics2 = sensors.poll_system_metrics()

        # Cache should return original value (not mutated)
        assert metrics2["perf_system_cpu_load"] == 10.5


def test_poll_system_metrics_thread_safety():
    """Test that cache is thread-safe under concurrent access."""
    call_counts = {"base": 0}

    def mock_base_metrics():
        """Mock that increments counter (not thread-safe itself)."""
        call_counts["base"] += 1
        return {
            "perf_system_cpu_load": 10.5,
            "perf_system_mem_used": 50.2,
        }

    with (
        patch(
            "personal_agent.brainstem.sensors.platforms.base.poll_base_metrics",
            side_effect=mock_base_metrics,
        ),
        patch("personal_agent.brainstem.sensors.sensors._get_platform_sensors") as mock_platform,
    ):
        mock_platform.return_value = None

        # Launch multiple threads accessing cache concurrently
        threads = []
        results = []

        def poll_and_store():
            result = sensors.poll_system_metrics()
            results.append(result)

        for _ in range(10):
            thread = threading.Thread(target=poll_and_store)
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        # All threads should get results
        assert len(results) == 10

        # All results should be equal
        for result in results:
            assert result == results[0]

        # Due to caching, base should be called much less than 10 times
        # (at most a few times due to race conditions, but definitely < 10)
        assert call_counts["base"] < 5


def test_get_system_metrics_snapshot_cache_independent():
    """Test that snapshot cache is independent from poll cache."""
    with (
        patch("personal_agent.brainstem.sensors.platforms.base.poll_base_metrics") as mock_base,
        patch(
            "personal_agent.brainstem.sensors.platforms.base.get_base_metrics_detailed"
        ) as mock_detailed,
        patch("personal_agent.brainstem.sensors.sensors._get_platform_sensors") as mock_platform,
    ):
        # Setup mocks
        mock_base.return_value = {
            "perf_system_cpu_load": 10.5,
            "perf_system_mem_used": 50.2,
        }
        mock_detailed.return_value = {
            "perf_system_cpu_load": 10.5,
            "perf_system_mem_used": 50.2,
            "perf_system_cpu_count": 8,
        }
        mock_platform.return_value = None

        # Call poll (uses "system" cache key)
        sensors.poll_system_metrics()
        assert mock_base.call_count == 1

        # Call snapshot (uses "snapshot" cache key)
        sensors.get_system_metrics_snapshot()
        assert mock_detailed.call_count == 1

        # Caches are independent, so both were called once


def test_get_system_metrics_snapshot_cache_hit():
    """Test that snapshot function uses cache on repeated calls."""
    with (
        patch(
            "personal_agent.brainstem.sensors.platforms.base.get_base_metrics_detailed"
        ) as mock_detailed,
        patch("personal_agent.brainstem.sensors.sensors._get_platform_sensors") as mock_platform,
    ):
        # Setup mocks
        mock_detailed.return_value = {
            "perf_system_cpu_load": 10.5,
            "perf_system_mem_used": 50.2,
            "perf_system_cpu_count": 8,
        }
        mock_platform.return_value = None

        # First call (cache miss)
        metrics1 = sensors.get_system_metrics_snapshot()
        assert mock_detailed.call_count == 1

        # Second call within TTL (cache hit)
        metrics2 = sensors.get_system_metrics_snapshot()

        # Should still be called only once
        assert mock_detailed.call_count == 1
        assert metrics1 == metrics2


def test_cache_with_platform_specific_metrics():
    """Test that cache works with platform-specific metrics (GPU)."""

    # Create proper mock class with method
    class MockPlatform:
        def poll_apple_metrics(self):
            return {"perf_system_gpu_load": 5.5}

    with (
        patch("personal_agent.brainstem.sensors.platforms.base.poll_base_metrics") as mock_base,
        patch(
            "personal_agent.brainstem.sensors.sensors._get_platform_sensors",
            return_value=MockPlatform(),
        ),
    ):
        # Setup mocks
        mock_base.return_value = {
            "perf_system_cpu_load": 10.5,
            "perf_system_mem_used": 50.2,
        }

        # First call (cache miss)
        metrics1 = sensors.poll_system_metrics()
        assert mock_base.call_count == 1
        assert metrics1["perf_system_gpu_load"] == 5.5

        # Second call (cache hit) - should include GPU metrics from cache
        metrics2 = sensors.poll_system_metrics()
        assert mock_base.call_count == 1
        assert metrics2["perf_system_gpu_load"] == 5.5


def test_cache_handles_platform_errors():
    """Test that cache works even when platform-specific polling fails."""

    # Create proper mock class with method that raises
    class MockPlatform:
        def poll_apple_metrics(self):
            raise RuntimeError("GPU poll failed")

    with (
        patch("personal_agent.brainstem.sensors.platforms.base.poll_base_metrics") as mock_base,
        patch(
            "personal_agent.brainstem.sensors.sensors._get_platform_sensors",
            return_value=MockPlatform(),
        ),
    ):
        # Setup mocks
        mock_base.return_value = {
            "perf_system_cpu_load": 10.5,
            "perf_system_mem_used": 50.2,
        }

        # Call should succeed even if platform metrics fail
        metrics = sensors.poll_system_metrics()

        # Should have base metrics
        assert metrics["perf_system_cpu_load"] == 10.5
        assert metrics["perf_system_mem_used"] == 50.2

        # Should not have GPU metrics (error was caught)
        assert "perf_system_gpu_load" not in metrics

        # Cache should still work on second call
        _ = sensors.poll_system_metrics()
        assert mock_base.call_count == 1  # Cache hit
