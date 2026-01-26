"""Deterministic metrics extraction for Captain's Log.

This module provides pure functions to extract metrics from the metrics_summary
dict without LLM involvement. This ensures:
- 100% deterministic output (same input → same output)
- 0% parse failures (no LLM formatting errors)
- Fast extraction (no LLM latency)
- Consistent format (standardized metric names and units)

The metrics_summary dict comes from RequestMonitor (ADR-0012) and contains
typed, validated values. We simply extract and format them according to
ADR-0014 conventions.

Related:
- ADR-0012: Request-Scoped Metrics Monitoring
- ADR-0014: Structured Metrics in Captain's Log
- ADR-0015: Tool Call Performance Optimization
"""

from typing import Any

from personal_agent.captains_log.models import Metric


def extract_metrics_from_summary(
    metrics_summary: dict[str, Any] | None,
) -> tuple[list[str], list[Metric]]:
    """Extract both human-readable strings and structured metrics from metrics_summary.

    This function is deterministic - no LLM involved. It extracts metrics from
    the typed dict returned by RequestMonitor and formats them according to
    ADR-0014 standardized naming conventions.

    Args:
        metrics_summary: Dict from RequestMonitor with typed metric values.
            Expected keys:
            - duration_seconds: float (request duration)
            - cpu_avg: float (average CPU % during request)
            - memory_avg: float (average memory % during request)
            - gpu_avg: float (average GPU % during request, Apple Silicon only)
            - samples_collected: int (number of monitoring samples)
            - threshold_violations: list[str] (metrics that exceeded thresholds)

    Returns:
        Tuple of (string_metrics, structured_metrics):
        - string_metrics: Human-readable list like ["cpu: 9.3%", "duration: 5.4s"]
        - structured_metrics: List of Metric objects with typed values

    Examples:
        >>> summary = {"duration_seconds": 5.4, "cpu_avg": 9.3}
        >>> strings, structured = extract_metrics_from_summary(summary)
        >>> strings
        ['duration: 5.4s', 'cpu: 9.3%']
        >>> structured[0]
        Metric(name='duration_seconds', value=5.4, unit='s')
    """
    if not metrics_summary:
        return [], []

    string_metrics: list[str] = []
    structured_metrics: list[Metric] = []

    # Duration (always present if RequestMonitor ran)
    if "duration_seconds" in metrics_summary:
        dur = float(metrics_summary["duration_seconds"])
        string_metrics.append(f"duration: {dur:.1f}s")
        structured_metrics.append(Metric(name="duration_seconds", value=dur, unit="s"))

    # CPU (always present)
    if "cpu_avg" in metrics_summary:
        cpu = float(metrics_summary["cpu_avg"])
        string_metrics.append(f"cpu: {cpu:.1f}%")
        structured_metrics.append(Metric(name="cpu_percent", value=cpu, unit="%"))

    # Memory (always present)
    if "memory_avg" in metrics_summary:
        mem = float(metrics_summary["memory_avg"])
        string_metrics.append(f"memory: {mem:.1f}%")
        structured_metrics.append(Metric(name="memory_percent", value=mem, unit="%"))

    # GPU (Apple Silicon only)
    if "gpu_avg" in metrics_summary:
        gpu = float(metrics_summary["gpu_avg"])
        string_metrics.append(f"gpu: {gpu:.1f}%")
        structured_metrics.append(Metric(name="gpu_percent", value=gpu, unit="%"))

    # Samples collected
    if "samples_collected" in metrics_summary:
        samples = int(metrics_summary["samples_collected"])
        string_metrics.append(f"samples: {samples}")
        structured_metrics.append(Metric(name="samples_collected", value=samples, unit=None))

    # Threshold violations (if any)
    if "threshold_violations" in metrics_summary:
        violations = metrics_summary["threshold_violations"]
        if isinstance(violations, list) and len(violations) > 0:
            count = len(violations)
            string_metrics.append(f"threshold_violations: {count}")
            structured_metrics.append(Metric(name="threshold_violations", value=count, unit=None))

    # CPU peak (if present)
    if "cpu_peak" in metrics_summary:
        cpu_peak = float(metrics_summary["cpu_peak"])
        string_metrics.append(f"cpu_peak: {cpu_peak:.1f}%")
        structured_metrics.append(Metric(name="cpu_peak_percent", value=cpu_peak, unit="%"))

    # Memory peak (if present)
    if "memory_peak" in metrics_summary:
        mem_peak = float(metrics_summary["memory_peak"])
        string_metrics.append(f"memory_peak: {mem_peak:.1f}%")
        structured_metrics.append(Metric(name="memory_peak_percent", value=mem_peak, unit="%"))

    # GPU peak (if present)
    if "gpu_peak" in metrics_summary:
        gpu_peak = float(metrics_summary["gpu_peak"])
        string_metrics.append(f"gpu_peak: {gpu_peak:.1f}%")
        structured_metrics.append(Metric(name="gpu_peak_percent", value=gpu_peak, unit="%"))

    return string_metrics, structured_metrics


def format_metrics_string(string_metrics: list[str]) -> str:
    """Format list of string metrics as comma-separated string.

    Convenience function for passing to LLM prompts.

    Args:
        string_metrics: List of human-readable metrics like ["cpu: 9.3%", "duration: 5.4s"]

    Returns:
        Comma-separated string like "cpu: 9.3%, duration: 5.4s"

    Examples:
        >>> format_metrics_string(["cpu: 9.3%", "duration: 5.4s"])
        'cpu: 9.3%, duration: 5.4s'
        >>> format_metrics_string([])
        'No metrics available'
    """
    if not string_metrics:
        return "No metrics available"
    return ", ".join(string_metrics)
