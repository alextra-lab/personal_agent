"""Stage 3: Governance.

Wraps the brainstem mode_manager to produce a GovernanceContext.
Per ADR-0063 §D1 (FRE-260), governance is mode-only; TaskType no longer
filters the tool payload — per-tool ``allowed_in_modes`` is the gate.
"""

from __future__ import annotations

import structlog

from personal_agent.governance.models import Mode
from personal_agent.request_gateway.types import GovernanceContext

logger = structlog.get_logger(__name__)

# Modes that disable expansion (resource pressure or safety concern)
_EXPANSION_DISABLED_MODES: frozenset[Mode] = frozenset(
    {Mode.ALERT, Mode.DEGRADED, Mode.LOCKDOWN, Mode.RECOVERY}
)


def evaluate_governance(
    mode: Mode = Mode.NORMAL,
    expansion_budget: int = 3,
) -> GovernanceContext:
    """Evaluate governance constraints for this request.

    Args:
        mode: Current brainstem operational mode.
        expansion_budget: Remaining expansion slots for this request.

    Returns:
        GovernanceContext with mode, expansion permission, and budget.
    """
    expansion_permitted = mode not in _EXPANSION_DISABLED_MODES

    logger.debug(
        "governance_evaluated",
        mode=mode.value,
        expansion_permitted=expansion_permitted,
        expansion_budget=expansion_budget,
    )

    return GovernanceContext(
        mode=mode,
        expansion_permitted=expansion_permitted,
        expansion_budget=expansion_budget,
    )
