"""Governance module for policy enforcement and operational modes.

This module provides:
- Governance configuration loading and validation
- Mode definitions and transition rules
- Tool permissions and policies
- Model constraints per mode
- Safety policies (content filtering, rate limits)
"""

import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type checking only - avoid circular import
    from personal_agent.config import GovernanceConfigError, load_governance_config
else:
    # Lazy import to avoid circular dependency at runtime
    # Re-export from config module with deprecation warning
    # TODO: Remove in v0.2.0 - use `from personal_agent.config import load_governance_config` instead
    def __getattr__(name: str):
        if name in ("GovernanceConfigError", "load_governance_config"):
            import personal_agent.config

            warnings.warn(
                "Importing load_governance_config from personal_agent.governance is deprecated. "
                "Use 'from personal_agent.config import load_governance_config' instead. "
                "This will be removed in v0.2.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            return getattr(personal_agent.config, name)
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


from personal_agent.governance.models import (
    ContentFiltering,
    GovernanceConfig,
    HumanApproval,
    HumanApprovalRule,
    Mode,
    ModeDefinition,
    ModelRoleConstraints,
    ModeModelConstraints,
    ModeThresholds,
    OutboundGateway,
    RateLimits,
    SafetyConfig,
    SecretPattern,
    ToolCategory,
    ToolPolicy,
    TransitionCondition,
    TransitionRule,
)

__all__ = [
    # Main exports
    "load_governance_config",
    "GovernanceConfigError",
    "GovernanceConfig",
    # Enums
    "Mode",
    # Models
    "ModeDefinition",
    "ModeThresholds",
    "TransitionRule",
    "TransitionCondition",
    "ToolCategory",
    "ToolPolicy",
    "ModeModelConstraints",
    "ModelRoleConstraints",
    "SafetyConfig",
    "ContentFiltering",
    "SecretPattern",
    "OutboundGateway",
    "RateLimits",
    "HumanApproval",
    "HumanApprovalRule",
]
