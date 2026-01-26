"""Base/fallback sensor implementation using psutil.

This module provides cross-platform sensor polling using psutil.
It serves as the base implementation and fallback for platforms
without specific implementations.
"""

from typing import Any

from personal_agent.telemetry import get_logger

log = get_logger(__name__)

# Try to import psutil, but make it optional
try:
    import psutil  # type: ignore[import-untyped]

    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    log.warning(
        "psutil_not_available", message="psutil not installed, sensor polling will be limited"
    )


def poll_base_metrics() -> dict[str, Any]:
    """Poll base system metrics using psutil.

    This provides cross-platform metrics that work on any system
    where psutil is available (CPU, memory, disk).

    Returns:
        Dictionary of base metrics:
        - perf_system_cpu_load: CPU usage percentage
        - perf_system_mem_used: Memory usage percentage
        - perf_system_disk_used: Disk usage percentage (root filesystem)
    """
    if not PSUTIL_AVAILABLE:
        log.debug("psutil_not_available_for_base_metrics")
        return {}

    metrics: dict[str, Any] = {}

    try:
        # CPU load (percentage)
        cpu_percent = psutil.cpu_percent(interval=0.1)  # Non-blocking quick sample
        metrics["perf_system_cpu_load"] = cpu_percent

        # Memory usage (percentage)
        memory = psutil.virtual_memory()
        metrics["perf_system_mem_used"] = memory.percent

        # Disk usage (percentage) - use root filesystem
        try:
            disk = psutil.disk_usage("/")
            metrics["perf_system_disk_used"] = (disk.used / disk.total) * 100.0
        except (OSError, PermissionError):
            # May not have permission on some systems
            log.debug("disk_usage_unavailable", message="Could not read disk usage")

        return metrics
    except Exception as e:
        log.error("base_metrics_poll_error", error=str(e), error_type=type(e).__name__)
        return {}


def get_base_metrics_detailed() -> dict[str, Any]:
    """Get detailed base metrics snapshot.

    Returns:
        Dictionary with additional detailed metrics:
        - perf_system_cpu_load: CPU usage percentage
        - perf_system_cpu_count: Number of CPU cores
        - perf_system_load_avg: Load average (if available)
        - perf_system_mem_used: Memory usage percentage
        - perf_system_mem_total_gb: Total memory in GB
        - perf_system_mem_available_gb: Available memory in GB
        - perf_system_disk_used: Disk usage percentage
        - perf_system_disk_total_gb: Total disk space in GB
        - perf_system_disk_free_gb: Free disk space in GB
    """
    if not PSUTIL_AVAILABLE:
        log.debug("psutil_not_available_for_detailed_metrics")
        return {}

    metrics: dict[str, Any] = {}

    try:
        # CPU metrics
        cpu_percent = psutil.cpu_percent(interval=0.1)
        cpu_count = psutil.cpu_count()
        load_avg = psutil.getloadavg() if hasattr(psutil, "getloadavg") else None

        metrics["perf_system_cpu_load"] = cpu_percent
        metrics["perf_system_cpu_count"] = cpu_count
        if load_avg:
            metrics["perf_system_load_avg"] = load_avg

        # Memory metrics
        memory = psutil.virtual_memory()
        metrics["perf_system_mem_used"] = memory.percent
        metrics["perf_system_mem_total_gb"] = memory.total / (1024**3)
        metrics["perf_system_mem_available_gb"] = memory.available / (1024**3)

        # Disk metrics
        try:
            disk = psutil.disk_usage("/")
            metrics["perf_system_disk_used"] = (disk.used / disk.total) * 100.0
            metrics["perf_system_disk_total_gb"] = disk.total / (1024**3)
            metrics["perf_system_disk_free_gb"] = disk.free / (1024**3)
        except (OSError, PermissionError):
            log.debug("disk_usage_unavailable")

        return metrics
    except Exception as e:
        log.error("base_metrics_detailed_error", error=str(e), error_type=type(e).__name__)
        return {}
