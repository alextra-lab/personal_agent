"""Stage 3: Governance.

Wraps the existing brainstem mode_manager to produce a GovernanceContext.
In Slice 1, this is a thin wrapper. Resource-aware gating and cost
budgeting are added in Slice 2.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
import yaml  # type: ignore[import-untyped]

from personal_agent.governance.models import Mode
from personal_agent.request_gateway.types import GovernanceContext, TaskType

logger = structlog.get_logger(__name__)

# Modes that disable expansion (resource pressure or safety concern)
_EXPANSION_DISABLED_MODES: frozenset[Mode] = frozenset(
    {Mode.ALERT, Mode.DEGRADED, Mode.LOCKDOWN, Mode.RECOVERY}
)


@lru_cache(maxsize=1)
def _load_tools_policies() -> tuple[dict[str, Any], dict[str, Any]]:
    """Load task_type_policies and mode_policies from tools.yaml (cached)."""
    from personal_agent.config import settings  # noqa: PLC0415

    config_path = settings.governance_config_path
    if not config_path.is_absolute():
        project_root = Path(__file__).parent.parent.parent.parent
        config_dir = (project_root / config_path).resolve()
    else:
        config_dir = config_path.resolve()

    tools_file = config_dir / "tools.yaml"
    with tools_file.open() as f:
        data: dict[str, Any] = yaml.safe_load(f)

    return (
        data.get("task_type_policies", {}),
        data.get("mode_policies", {}),
    )


def evaluate_governance(
    mode: Mode = Mode.NORMAL,
    expansion_budget: int = 3,
    task_type: TaskType | None = None,
) -> GovernanceContext:
    """Evaluate governance constraints for this request.

    Args:
        mode: Current brainstem operational mode.
        expansion_budget: Remaining expansion slots for this request.
        task_type: Classified task type (from Stage 4). When provided,
            allowed_tool_categories is computed as the intersection of the
            task-type policy and the mode policy from tools.yaml. When None,
            allowed_tool_categories is None (no restriction signal).

    Returns:
        GovernanceContext with mode, expansion permission, budget, and
        optionally the allowed tool categories.
    """
    expansion_permitted = mode not in _EXPANSION_DISABLED_MODES

    allowed_tool_categories: list[str] | None = None
    if task_type is not None:
        task_type_policies, mode_policies = _load_tools_policies()
        task_cats = set(
            task_type_policies.get(task_type.value, {}).get("allowed_categories", [])
        )
        mode_cats = set(mode_policies.get(mode.value, {}).get("allowed_categories", []))
        allowed_tool_categories = sorted(task_cats & mode_cats)

    logger.debug(
        "governance_evaluated",
        mode=mode.value,
        expansion_permitted=expansion_permitted,
        expansion_budget=expansion_budget,
        task_type=task_type.value if task_type else None,
        allowed_tool_categories=allowed_tool_categories,
    )

    return GovernanceContext(
        mode=mode,
        expansion_permitted=expansion_permitted,
        expansion_budget=expansion_budget,
        allowed_tool_categories=allowed_tool_categories,
    )
