"""Brainstem expansion signals — expansion budget and contraction trigger.

The expansion_budget signal tells the gateway how many concurrent sub-agents
are safe to run. The contraction trigger detects when expansion is complete
and the system should return to calm state.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.8
"""

from __future__ import annotations

from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Thresholds for resource pressure
_CPU_HIGH = 70.0
_CPU_CRITICAL = 90.0
_MEMORY_HIGH = 75.0
_MEMORY_CRITICAL = 90.0


class ContractionState(Enum):
    """System contraction readiness."""

    EXPANDING = "expanding"
    BUSY = "busy"
    COOLING = "cooling"
    READY = "ready"


def compute_expansion_budget(
    metrics: dict[str, Any],
    max_budget: int = 3,
) -> int:
    """Compute how many concurrent sub-agents are safe to run.

    Args:
        metrics: System metrics from brainstem sensors.
            Expected keys: cpu_percent, memory_percent, active_inference_count.
        max_budget: Maximum expansion budget when fully calm.

    Returns:
        Number of safe concurrent sub-agents (0 to max_budget).
    """
    cpu = metrics.get("cpu_percent")
    memory = metrics.get("memory_percent")
    active = metrics.get("active_inference_count")

    if cpu is None or memory is None or active is None:
        logger.warning(
            "expansion_budget_missing_metrics",
            metrics=list(metrics.keys()),
        )
        return 0

    budget = max_budget

    if cpu >= _CPU_CRITICAL:
        budget = 0
    elif cpu >= _CPU_HIGH:
        budget = min(budget, 1)

    if memory >= _MEMORY_CRITICAL:
        budget = 0
    elif memory >= _MEMORY_HIGH:
        budget = min(budget, 1)

    if active >= 2:
        budget = 0
    elif active >= 1:
        budget = min(budget, 1)

    logger.debug(
        "expansion_budget_computed",
        cpu_percent=cpu,
        memory_percent=memory,
        active_inference=active,
        budget=budget,
    )

    return max(0, budget)


def detect_contraction(
    active_sub_agents: int,
    pending_requests: int,
    idle_seconds: float,
    idle_threshold: float = 30.0,
) -> ContractionState:
    """Detect whether the system is ready to contract.

    Args:
        active_sub_agents: Currently running sub-agent tasks.
        pending_requests: Queued user requests.
        idle_seconds: Seconds since last activity.
        idle_threshold: Minimum idle seconds before contraction.

    Returns:
        ContractionState indicating readiness.
    """
    if active_sub_agents > 0:
        return ContractionState.EXPANDING
    if pending_requests > 0:
        return ContractionState.BUSY
    if idle_seconds < idle_threshold:
        return ContractionState.COOLING
    return ContractionState.READY
