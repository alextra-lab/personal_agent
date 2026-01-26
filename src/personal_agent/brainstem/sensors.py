"""Sensor polling for system metrics.

This module provides the main sensor polling API that detects the platform
and delegates to platform-specific implementations.

Platform-specific sensors are in the `sensors/platforms/` submodule:
- `base.py`: Cross-platform metrics using psutil (CPU, memory, disk)
- `apple.py`: Apple Silicon-specific metrics (GPU via powermetrics)

The main functions (`poll_system_metrics`, `get_system_metrics_snapshot`)
automatically detect the platform and combine base + platform-specific metrics.
"""

import platform
from typing import Any

from personal_agent.telemetry import SENSOR_POLL, SYSTEM_METRICS_SNAPSHOT, get_logger

log = get_logger(__name__)


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
    metrics: dict[str, Any] = {}

    # Get base metrics (cross-platform, uses psutil)
    from personal_agent.brainstem.sensors.platforms.base import poll_base_metrics

    base_metrics = poll_base_metrics()
    metrics.update(base_metrics)

    # Get platform-specific metrics
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

    # Log sensor poll event
    log.debug(
        SENSOR_POLL,
        cpu_load=metrics.get("perf_system_cpu_load"),
        memory_used=metrics.get("perf_system_mem_used"),
        gpu_load=metrics.get("perf_system_gpu_load"),
        platform=_detect_platform(),
        metrics_count=len(metrics),
    )

    return metrics


def get_system_metrics_snapshot() -> dict[str, Any]:
    """Get a comprehensive system metrics snapshot.

    This is a more detailed version of poll_system_metrics() that includes
    additional metrics and emits a SYSTEM_METRICS_SNAPSHOT event.

    Returns:
        Dictionary of system metrics with additional details:
        - Base metrics: CPU, memory, disk (detailed)
        - Platform-specific metrics: GPU, etc. (if available)
    """
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
            log.debug(
                "platform_metrics_error",
                platform=_detect_platform(),
                error=str(e),
                error_type=type(e).__name__,
            )

    # Emit snapshot event
    log.info(
        SYSTEM_METRICS_SNAPSHOT,
        cpu_load=metrics.get("perf_system_cpu_load"),
        memory_used=metrics.get("perf_system_mem_used"),
        cpu_count=metrics.get("perf_system_cpu_count"),
        gpu_load=metrics.get("perf_system_gpu_load"),
        platform=_detect_platform(),
        metrics_count=len(metrics),
    )

    return metrics
