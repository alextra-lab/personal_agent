"""Sensor polling for system metrics.

This module provides the main sensor polling API that detects the platform
and delegates to platform-specific implementations.

Platform-specific sensors are in the `sensors/platforms/` submodule:
- `base.py`: Cross-platform metrics using psutil (CPU, memory, disk)
- `apple.py`: Apple Silicon-specific metrics (GPU via powermetrics)

The main functions (`poll_system_metrics`, `get_system_metrics_snapshot`)
automatically detect the platform and combine base + platform-specific metrics.

Caching:
- Module-level cache with configurable TTL (default 10s)
- Transparent to callers (no coupling between consumers)
- Thread-safe (protected by lock)
- Especially important for GPU metrics (macmon polls are expensive: ~3.6s)
"""

import platform
import threading
import time
from typing import Any

from personal_agent.telemetry import SENSOR_POLL, SYSTEM_METRICS_SNAPSHOT, get_logger

log = get_logger(__name__)

# Sensor-level cache (ADR-0014, ADR-0015)
# This cache is transparent to consumers (RequestMonitor, tools, etc.)
# and avoids expensive repeated polls to hardware sensors.
_METRICS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 10.0  # Cache TTL (2x RequestMonitor polling interval)
_cache_lock = threading.Lock()


def _detect_platform() -> str:
    """Detect the current platform.

    Returns:
        Platform identifier: 'apple' for Apple Silicon, 'generic' otherwise.
    """
    if platform.machine() == "arm64" and platform.system() == "Darwin":
        return "apple"
    return "generic"


def _get_platform_sensors() -> Any:
    """Get the platform-specific sensor module.

    Returns:
        Platform sensor module with poll_platform_metrics() function.
    """
    platform_name = _detect_platform()

    if platform_name == "apple":
        from personal_agent.brainstem.sensors.platforms import apple

        return apple
    else:
        # For generic/unknown platforms, return None (only base metrics)
        return None


def poll_system_metrics() -> dict[str, Any]:
    """Poll system metrics (CPU, memory, disk, GPU if available).

    This function automatically detects the platform and combines:
    - Base metrics (CPU, memory, disk) from psutil
    - Platform-specific metrics (e.g., GPU on Apple Silicon)

    Includes automatic caching (10s TTL) to avoid expensive repeated polls
    to hardware sensors (especially GPU via macmon/powermetrics: ~3.6s).

    The caching is transparent to callers - both RequestMonitor and tools
    benefit without creating coupling between them.

    Returns a dictionary of sensor metrics keyed by metric ID as defined
    in CONTROL_LOOPS_SENSORS_v0.1.md.

    Returns:
        Dictionary of sensor metrics. Example:
        {
            "perf_system_cpu_load": 45.2,
            "perf_system_mem_used": 62.5,
            "perf_system_disk_used": 78.1,
            "perf_system_gpu_load": 15.3,  # Platform-specific
        }
    """
    # Check cache first (fast path)
    with _cache_lock:
        if "system" in _METRICS_CACHE:
            timestamp, cached_metrics = _METRICS_CACHE["system"]
            age = time.time() - timestamp
            if age < _CACHE_TTL_SECONDS:
                log.debug(
                    "sensor_cache_hit", age_seconds=round(age, 2), ttl_seconds=_CACHE_TTL_SECONDS
                )
                return cached_metrics.copy()

    # Cache miss or expired - poll hardware (slow path)
    log.debug("sensor_cache_miss", reason="expired or empty", ttl_seconds=_CACHE_TTL_SECONDS)

    metrics: dict[str, Any] = {}

    # Get base metrics (cross-platform, uses psutil, fast: <10ms)
    from personal_agent.brainstem.sensors.platforms.base import poll_base_metrics

    base_metrics = poll_base_metrics()
    metrics.update(base_metrics)

    # Get platform-specific metrics (slow: ~3.6s for GPU on Apple Silicon)
    platform_sensors = _get_platform_sensors()
    if platform_sensors:
        try:
            platform_metrics = platform_sensors.poll_apple_metrics()
            metrics.update(platform_metrics)
        except Exception as e:
            log.debug(
                "platform_metrics_error",
                platform=_detect_platform(),
                error=str(e),
                error_type=type(e).__name__,
            )

    # Update cache
    with _cache_lock:
        _METRICS_CACHE["system"] = (time.time(), metrics.copy())

    # Log sensor poll event
    log.debug(
        SENSOR_POLL,
        cpu_load=metrics.get("perf_system_cpu_load"),
        memory_used=metrics.get("perf_system_mem_used"),
        gpu_load=metrics.get("perf_system_gpu_load"),
        platform=_detect_platform(),
        metrics_count=len(metrics),
        cache_updated=True,
    )

    return metrics


def get_system_metrics_snapshot() -> dict[str, Any]:
    """Get a comprehensive system metrics snapshot.

    This is a more detailed version of poll_system_metrics() that includes
    additional metrics and emits a SYSTEM_METRICS_SNAPSHOT event.

    Includes automatic caching (10s TTL) to avoid expensive repeated polls
    to hardware sensors. This is especially important when tools call this
    function while RequestMonitor is already polling in the background.

    Returns:
        Dictionary of system metrics with additional details:
        - Base metrics: CPU, memory, disk (detailed)
        - Platform-specific metrics: GPU, etc. (if available)
    """
    # Check cache first (fast path)
    # Use "snapshot" key to differentiate from poll_system_metrics() cache
    with _cache_lock:
        if "snapshot" in _METRICS_CACHE:
            timestamp, cached_metrics = _METRICS_CACHE["snapshot"]
            age = time.time() - timestamp
            if age < _CACHE_TTL_SECONDS:
                log.debug(
                    "sensor_snapshot_cache_hit",
                    age_seconds=round(age, 2),
                    ttl_seconds=_CACHE_TTL_SECONDS,
                )
                # Still emit event (tools expect this)
                log.info(
                    SYSTEM_METRICS_SNAPSHOT,
                    cpu_load=cached_metrics.get("perf_system_cpu_load"),
                    memory_used=cached_metrics.get("perf_system_mem_used"),
                    cpu_count=cached_metrics.get("perf_system_cpu_count"),
                    gpu_load=cached_metrics.get("perf_system_gpu_load"),
                    platform=_detect_platform(),
                    metrics_count=len(cached_metrics),
                    cache_hit=True,
                )
                return cached_metrics.copy()

    # Cache miss or expired - poll hardware (slow path)
    log.debug(
        "sensor_snapshot_cache_miss", reason="expired or empty", ttl_seconds=_CACHE_TTL_SECONDS
    )

    metrics: dict[str, Any] = {}

    # Get detailed base metrics
    from personal_agent.brainstem.sensors.platforms.base import get_base_metrics_detailed

    base_metrics = get_base_metrics_detailed()
    metrics.update(base_metrics)

    # Get platform-specific metrics
    platform_sensors = _get_platform_sensors()
    if platform_sensors:
        try:
            platform_metrics = platform_sensors.poll_apple_metrics()
            metrics.update(platform_metrics)
        except Exception as e:
            log.warning(
                "platform_metrics_error",
                platform=_detect_platform(),
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )

    # Update cache
    with _cache_lock:
        _METRICS_CACHE["snapshot"] = (time.time(), metrics.copy())

    # Emit snapshot event
    log.info(
        SYSTEM_METRICS_SNAPSHOT,
        cpu_load=metrics.get("perf_system_cpu_load"),
        memory_used=metrics.get("perf_system_mem_used"),
        cpu_count=metrics.get("perf_system_cpu_count"),
        gpu_load=metrics.get("perf_system_gpu_load"),
        platform=_detect_platform(),
        metrics_count=len(metrics),
        cache_hit=False,
    )

    return metrics
