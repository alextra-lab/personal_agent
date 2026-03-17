"""Stage 3: Governance.

Wraps the existing brainstem mode_manager to produce a GovernanceContext.
In Slice 1, this is a thin wrapper. Resource-aware gating and cost
budgeting are added in Slice 2.
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
) -> GovernanceContext:
    """Evaluate governance constraints for this request.

    Args:
        mode: Current brainstem operational mode.

    Returns:
        GovernanceContext with mode and expansion permission.
    """
    expansion_permitted = mode not in _EXPANSION_DISABLED_MODES

    logger.debug(
        "governance_evaluated",
        mode=mode.value,
        expansion_permitted=expansion_permitted,
    )

    return GovernanceContext(
        mode=mode,
        expansion_permitted=expansion_permitted,
    )
