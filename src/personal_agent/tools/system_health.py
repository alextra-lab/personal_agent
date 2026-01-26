"""System health tools for monitoring system metrics.

This module provides tools for querying system health metrics like CPU,
memory, disk, and GPU usage.
"""

from typing import Any

from personal_agent.brainstem.sensors import get_system_metrics_snapshot
from personal_agent.tools.types import ToolDefinition


def system_metrics_snapshot_executor() -> dict[str, Any]:
    """Execute system_metrics_snapshot tool.

    Returns a comprehensive snapshot of system metrics including CPU, memory,
    disk, and GPU (if available).

    Returns:
        Dictionary with:
        - success: bool
        - metrics: dict (system metrics) or None if error
        - error: str or None
    """
    try:
        metrics = get_system_metrics_snapshot()

        return {
            "success": True,
            "metrics": metrics,
            "error": None,
        }

    except Exception as e:
        return {
            "success": False,
            "metrics": None,
            "error": f"Error getting system metrics: {e}",
        }


system_metrics_snapshot_tool = ToolDefinition(
    name="system_metrics_snapshot",
    description="Get a comprehensive snapshot of system metrics (CPU, memory, disk, GPU)",
    category="read_only",
    parameters=[],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=10,
    rate_limit_per_hour=None,
)
