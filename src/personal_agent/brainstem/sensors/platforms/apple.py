"""Apple Silicon-specific sensor implementation.

This module provides Apple Silicon (M-series) specific sensor polling,
including GPU metrics via multiple methods:

1. macmon (preferred): Uses private macOS APIs, no sudo required
   - Status: Broken on macOS 14+ ("Failed to create subscription")
2. powermetrics (production): Official Apple tool, requires sudo
   - Status: Recommended with sudoers configuration (no password prompts)
   - Setup: See docs/GPU_METRICS_SETUP.md

For LLM workloads, GPU metrics are critical for homeostasis control loops.
"""

import json
import platform
import subprocess
from typing import Any

from personal_agent.telemetry import get_logger

log = get_logger(__name__)


def is_apple_silicon() -> bool:
    """Check if running on Apple Silicon.

    Returns:
        True if on Apple Silicon (ARM64), False otherwise.
    """
    return platform.machine() == "arm64" and platform.system() == "Darwin"


def _poll_gpu_via_macmon() -> dict[str, Any]:
    """Poll GPU metrics using macmon (no sudo required).

    macmon uses private macOS APIs to access GPU metrics without requiring
    elevated privileges. This is the preferred method for security.

    Tries macmon-python package first (most reliable), falls back to subprocess
    if package not available.

    Returns:
        Dictionary with GPU metrics if available, empty dict otherwise.
        Keys:
        - perf_system_gpu_load: GPU utilization percentage (from gpu_usage[1])
        - perf_system_gpu_power_w: GPU power consumption in watts
        - perf_system_gpu_temp_c: GPU temperature in Celsius
    """
    metrics: dict[str, Any] = {}

    # Try macmon-python package first (most reliable)
    try:
        from macmon import MacMon  # type: ignore[import-untyped]

        macmon = MacMon()
        json_str = macmon.get_metrics()
        data = json.loads(json_str)

        # Extract GPU metrics
        if "gpu_power" in data:
            metrics["perf_system_gpu_power_w"] = float(data["gpu_power"])

        if (
            "gpu_usage" in data
            and isinstance(data["gpu_usage"], list)
            and len(data["gpu_usage"]) >= 2
        ):
            # gpu_usage is [frequency_mhz, utilization_ratio]
            utilization_ratio = float(data["gpu_usage"][1])
            metrics["perf_system_gpu_load"] = utilization_ratio * 100.0  # Convert to percentage

        if "temp" in data and isinstance(data["temp"], dict):
            if "gpu_temp_avg" in data["temp"]:
                metrics["perf_system_gpu_temp_c"] = float(data["temp"]["gpu_temp_avg"])

        if metrics:
            log.info(
                "gpu_metrics_collected_via_macmon_python",
                metrics=list(metrics.keys()),
                gpu_load=metrics.get("perf_system_gpu_load"),
            )
            return metrics
        else:
            log.warning(
                "macmon_python_no_metrics",
                message="macmon-python package imported successfully but returned no GPU metrics",
            )

    except ImportError:
        # macmon-python not installed, try subprocess approach
        log.warning(
            "macmon_python_not_available",
            message="macmon-python package not installed (trying subprocess fallback)",
        )
    except json.JSONDecodeError as e:
        log.warning("macmon_python_json_error", error=str(e), exc_info=True)
    except Exception as e:
        log.warning("macmon_python_error", error=str(e), error_type=type(e).__name__, exc_info=True)

    # Fallback: Try subprocess approach
    try:
        # Check if macmon is available
        which_result = subprocess.run(
            ["which", "macmon"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )

        if which_result.returncode != 0:
            log.debug("macmon_not_found", message="macmon not installed (brew install macmon)")
            return {}

        # Run macmon pipe to get JSON output
        # macmon pipe outputs JSON lines continuously
        # Try multiple approaches to capture output
        import time

        stdout = None
        stderr = None

        # Approach 1: Try with script command (provides TTY)
        try:
            result = subprocess.run(
                ["script", "-q", "/dev/null", "timeout", "1", "macmon", "pipe"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.stdout:
                lines = result.stdout.strip().split("\n")
                for line in lines:
                    line = line.strip()
                    if line and line.startswith("{"):
                        stdout = line
                        break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Approach 2: Direct subprocess with extended timeout
        if not stdout:
            proc = subprocess.Popen(
                ["macmon", "pipe"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(2.5)  # Give it time to output
            proc.terminate()

            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()

        if not stdout or not stdout.strip():
            stderr_text = stderr[:200] if stderr else None
            log.debug("macmon_no_output", stderr=stderr_text)
            return {}

        # Find first valid JSON line
        lines = stdout.strip().split("\n")
        data = None
        for line in lines:
            line = line.strip()
            if line and line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

        if not data:
            log.debug("macmon_invalid_json", preview=stdout[:200])
            return {}

        # Extract GPU metrics from macmon JSON structure
        # Structure: {"gpu_power": float, "gpu_usage": [freq, utilization], "temp": {"gpu_temp_avg": float}}
        if "gpu_power" in data:
            metrics["perf_system_gpu_power_w"] = float(data["gpu_power"])

        if (
            "gpu_usage" in data
            and isinstance(data["gpu_usage"], list)
            and len(data["gpu_usage"]) >= 2
        ):
            # gpu_usage is [frequency_mhz, utilization_ratio]
            utilization_ratio = float(data["gpu_usage"][1])
            metrics["perf_system_gpu_load"] = utilization_ratio * 100.0  # Convert to percentage

        if "temp" in data and isinstance(data["temp"], dict):
            if "gpu_temp_avg" in data["temp"]:
                metrics["perf_system_gpu_temp_c"] = float(data["temp"]["gpu_temp_avg"])

        if metrics:
            log.info(
                "gpu_metrics_collected_via_macmon",
                metrics=list(metrics.keys()),
                gpu_load=metrics.get("perf_system_gpu_load"),
            )
            return metrics

    except subprocess.TimeoutExpired:
        log.warning("macmon_timeout", message="macmon command timed out")
    except FileNotFoundError:
        log.warning("macmon_not_found", message="macmon command not found")
    except Exception as e:
        log.warning("macmon_poll_error", error=str(e), error_type=type(e).__name__, exc_info=True)

    return metrics


def _poll_gpu_via_powermetrics() -> dict[str, Any]:
    """Poll GPU metrics using powermetrics (requires sudo).

    Uses Apple's powermetrics tool to get GPU utilization, power, and temperature.
    Falls back gracefully if powermetrics is unavailable or requires elevated permissions.

    Note: powermetrics requires sudo privileges on macOS. To enable sudo-less access
    (no password prompts), configure sudoers as documented in docs/GPU_METRICS_SETUP.md.

    Returns:
        Dictionary with GPU metrics if available, empty dict otherwise.
    """
    metrics: dict[str, Any] = {}

    try:
        # Use sudo powermetrics directly (requires sudoers configuration)
        # powermetrics without sudo never works on modern macOS
        # See docs/GPU_METRICS_SETUP.md for setup instructions
        #
        # Command: sudo powermetrics -n 1 -i 1000 --samplers gpu_power --format json
        # -n 1: sample once
        # -i 1000: sample interval 1000ms
        # --samplers gpu_power: sample GPU power metrics
        # --format json: output as JSON
        result = subprocess.run(
            [
                "sudo",
                "powermetrics",
                "-n",
                "1",
                "-i",
                "1000",
                "--samplers",
                "gpu_power",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        if result.returncode != 0:
            # powermetrics failed - likely sudoers not configured
            stderr_preview = result.stderr[:200] if result.stderr else None

            if "password is required" in (result.stderr or ""):
                log.warning(
                    "powermetrics_requires_sudo_config",
                    message="powermetrics requires sudo access. Configure sudoers per docs/GPU_METRICS_SETUP.md",
                    stderr=stderr_preview,
                )
            else:
                log.debug(
                    "powermetrics_unavailable",
                    returncode=result.returncode,
                    stderr=stderr_preview,
                )
            return {}

        # Parse JSON output
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            log.debug("powermetrics_invalid_json", stdout_preview=result.stdout[:200])
            return {}

        # Extract GPU metrics from powermetrics output
        # Structure varies by macOS version, so we try multiple paths
        gpu_data = None

        # Try to find GPU metrics in the JSON structure
        if isinstance(data, dict):
            # Look for GPU power or GPU metrics
            if "gpu_power" in data:
                gpu_data = data["gpu_power"]
            elif "processor_power" in data:
                # Sometimes GPU is under processor_power
                proc_power = data["processor_power"]
                if isinstance(proc_power, dict) and "gpu" in proc_power:
                    gpu_data = proc_power["gpu"]
            elif "gpu" in data:
                gpu_data = data["gpu"]

        if gpu_data and isinstance(gpu_data, dict):
            # Extract utilization (if available)
            if "utilization" in gpu_data:
                metrics["perf_system_gpu_load"] = float(gpu_data["utilization"])
            elif "usage" in gpu_data:
                metrics["perf_system_gpu_load"] = float(gpu_data["usage"])

            # Extract power (if available)
            if "power" in gpu_data:
                metrics["perf_system_gpu_power_w"] = float(gpu_data["power"])

            # Extract temperature (if available)
            if "temperature" in gpu_data:
                metrics["perf_system_gpu_temp_c"] = float(gpu_data["temperature"])

        if metrics:
            log.debug(
                "gpu_metrics_collected",
                metrics=list(metrics.keys()),
                gpu_load=metrics.get("perf_system_gpu_load"),
            )

    except subprocess.TimeoutExpired:
        log.debug("powermetrics_timeout", message="powermetrics command timed out")
    except FileNotFoundError:
        log.debug("powermetrics_not_found", message="powermetrics command not found")
    except PermissionError:
        log.debug(
            "powermetrics_permission_denied",
            message="powermetrics requires sudo privileges",
        )
    except Exception as e:
        log.debug("gpu_poll_error", error=str(e), error_type=type(e).__name__)

    return metrics


def poll_apple_gpu_metrics() -> dict[str, Any]:
    """Poll Apple Silicon GPU metrics using secure methods.

    Tries multiple methods in order of preference:
    1. macmon (preferred): No sudo required, uses private macOS APIs
    2. powermetrics (fallback): Official tool, requires sudo

    Returns:
        Dictionary with GPU metrics if available, empty dict otherwise.
        Keys:
        - perf_system_gpu_load: GPU utilization percentage
        - perf_system_gpu_power_w: GPU power consumption in watts
        - perf_system_gpu_temp_c: GPU temperature in Celsius

    References:
        - psutil cannot access Apple Silicon GPU sensors
        - See docs/GPU_METRICS_SECURITY.md for security options
    """
    if not is_apple_silicon():
        return {}

    # Try macmon first (most secure, no sudo)
    metrics = _poll_gpu_via_macmon()
    if metrics:
        log.info("gpu_metrics_via_macmon", metrics=list(metrics.keys()))
        return metrics

    # Fallback to powermetrics (requires sudo, see docs/GPU_METRICS_SETUP.md)
    metrics = _poll_gpu_via_powermetrics()
    if metrics:
        log.info("gpu_metrics_via_powermetrics", metrics=list(metrics.keys()))
    else:
        log.warning(
            "gpu_metrics_unavailable",
            message="Neither macmon nor powermetrics provided GPU metrics. "
            "Configure sudo-less powermetrics access per docs/GPU_METRICS_SETUP.md",
        )

    return metrics


def poll_apple_metrics() -> dict[str, Any]:
    """Poll all Apple Silicon-specific metrics.

    This combines base metrics (from base.py) with Apple-specific GPU metrics.

    Returns:
        Dictionary of Apple-specific metrics. Currently includes GPU metrics
        if available. Base metrics (CPU, memory, disk) should be obtained
        separately via poll_base_metrics().
    """
    if not is_apple_silicon():
        return {}

    metrics: dict[str, Any] = {}

    # Poll GPU metrics
    gpu_metrics = poll_apple_gpu_metrics()
    metrics.update(gpu_metrics)

    return metrics
