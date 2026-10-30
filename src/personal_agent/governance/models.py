"""Pydantic models for governance configuration.

This module defines the schema for governance policies including:
- Operational modes (NORMAL, ALERT, DEGRADED, LOCKDOWN, RECOVERY)
- Mode transition rules
- Tool permissions and the sub-agent tool decisions
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Approval keys that ADR-0156 D8 (FRE-1574) retired. ``config/governance/permissions.yaml``
#: decides whether a call runs, asks or is refused. A tools.yaml entry that still holds one is
#: rejected, so that a stale key cannot read as if it still governed anything.
RETIRED_APPROVAL_KEYS: frozenset[str] = frozenset(
    {
        "requires_approval",
        "requires_approval_in_modes",
        "auto_approve_prefixes",
        "hard_deny_patterns",
    }
)


def _reject_retired_approval_keys(data: object, owner: str) -> object:
    """Reject the approval keys that permissions.yaml replaced.

    Args:
        data: The raw mapping before validation.
        owner: The model name, for the error message.

    Returns:
        The data unchanged.

    Raises:
        ValueError: When a retired key is present.
    """
    if isinstance(data, dict):
        retired = sorted(RETIRED_APPROVAL_KEYS & set(data))
        if retired:
            raise ValueError(
                f"{owner}: {retired} retired by ADR-0156 D8; write a rule in "
                "config/governance/permissions.yaml instead"
            )
    return data


class Mode(str, Enum):
    """Operational modes for the agent."""

    NORMAL = "NORMAL"
    ALERT = "ALERT"
    DEGRADED = "DEGRADED"
    LOCKDOWN = "LOCKDOWN"
    RECOVERY = "RECOVERY"


class ModeDefinition(BaseModel):
    """Definition of an operational mode."""

    description: str = Field(..., description="Human-readable description of the mode")


class TransitionCondition(BaseModel):
    """A single condition for mode transition."""

    metric: str = Field(..., description="Metric name to check")
    operator: str = Field(..., description="Comparison operator: >, <, ==, >=, <=")
    value: float | int = Field(..., description="Threshold value")
    duration_seconds: int | None = Field(
        None, ge=0, description="Duration condition must hold (for sustained metrics)"
    )
    window_seconds: int | None = Field(None, ge=0, description="Time window for counting events")


class TransitionRule(BaseModel):
    """Rule for transitioning between modes."""

    conditions: list[TransitionCondition] = Field(..., description="Conditions to check")
    logic: str = Field("any", description="Logic: 'any' (OR) or 'all' (AND) conditions must be met")
    requires_human_approval: bool = Field(
        False, description="Whether human approval is required for this transition"
    )


class ToolPolicy(BaseModel):
    """Policy for a specific tool."""

    category: str = Field(..., description="Tool category")
    allowed_in_modes: list[str] = Field(..., description="Modes where tool is allowed")
    forbidden_in_modes: list[str] = Field(
        default_factory=list, description="Modes where tool is forbidden"
    )
    max_file_size_mb: int | None = Field(
        None, ge=0, description="Maximum file size in MB (for file tools)"
    )
    allowed_paths: list[str] = Field(default_factory=list, description="Allowed path patterns")
    forbidden_paths: list[str] = Field(default_factory=list, description="Forbidden path patterns")
    unattended_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Path globs where 'write' may proceed without interactive approval "
            "(scratch / sandbox directories, e.g. /tmp/**, /app/agent_workspace/sandbox/**)"
        ),
    )
    allowed_commands: list[str] = Field(
        default_factory=list, description="Allowed command patterns"
    )
    forbidden_commands: list[str] = Field(
        default_factory=list, description="Forbidden command patterns"
    )
    requires_outbound_gateway: bool = Field(
        False, description="Whether outbound gateway is required"
    )
    rate_limit_per_hour: int | None = Field(None, ge=0, description="Rate limit per hour")
    loop_max_per_signature: int = Field(
        default=1,
        ge=0,
        description="Max executions of the same (tool, args) pair per request",
    )
    loop_max_consecutive: int = Field(
        default=3,
        ge=1,
        description="Consecutive calls before warning; hard-block fires at this + 1",
    )
    loop_output_sensitive: bool = Field(
        default=False,
        description="If True, skip output-identity blocking (for polling tools whose output changes each call)",
    )
    loop_consecutive_terminal: bool = Field(
        default=False,
        description=(
            "If True, WARN_CONSECUTIVE escalates to terminal BLOCK_CONSECUTIVE at loop_max_consecutive. "
            "For tools whose consecutive-different-args mode is dominantly pathological (e.g. query_elasticsearch "
            "guessing wrong index names, bash retrying unknown commands). Default False preserves ADR-0063 §D5."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _no_retired_keys(cls, data: object) -> object:
        """Reject the approval keys that permissions.yaml replaced (FRE-1574)."""
        return _reject_retired_approval_keys(data, "tool policy")


class SubAgentToolDecision(BaseModel):
    """One tool's recorded grant decision for the sub-agent principal (FRE-1463).

    A list of granted names can only record the tools that were said yes to. A
    refusal then reads as an absence, indistinguishable from a tool nobody has
    considered yet. This record carries both answers and the reason for each, so
    ``config/governance/tools.yaml`` states the decision rather than its result.

    Attributes:
        granted: Whether a sub-agent may use this tool.
        reason: Why the decision went that way. Required and non-blank — a reason
            that may be empty turns this record back into a list of names.
    """

    model_config = ConfigDict(frozen=True)

    granted: bool = Field(..., description="Whether a sub-agent may use this tool")
    reason: str = Field(..., min_length=1, description="Why this decision was made")
    param_ceilings: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per-parameter maximum values enforced only for the sub-agent principal "
            "(FRE-1473). A parameter absent here is unbounded for this principal; the "
            "primary's own tool policy never reads this mapping."
        ),
    )

    param_forced: dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "Per-parameter values pinned only for the sub-agent principal (FRE-1564). The "
            "dispatch boundary overwrites a supplied or omitted argument with the pinned "
            "value. The primary's own tool policy never reads this mapping."
        ),
    )

    @field_validator("reason")
    @classmethod
    def _reason_is_not_blank(cls, value: str) -> str:
        """Reject a whitespace-only reason.

        Args:
            value: The recorded reason.

        Returns:
            The reason unchanged.

        Raises:
            ValueError: When the reason holds no non-whitespace character.
        """
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value

    @field_validator("param_ceilings")
    @classmethod
    def _ceilings_are_named_and_positive(cls, value: dict[str, int]) -> dict[str, int]:
        """Reject a blank parameter name or a non-positive ceiling.

        Args:
            value: The recorded per-parameter ceilings.

        Returns:
            The ceilings unchanged.

        Raises:
            ValueError: A key is blank/whitespace-only, or a ceiling is not positive.
        """
        for param, ceiling in value.items():
            if not param.strip():
                raise ValueError("param_ceilings key must not be blank")
            if ceiling <= 0:
                raise ValueError(
                    f"param_ceilings[{param!r}] must be a positive integer, got {ceiling}"
                )
        return value

    @field_validator("param_forced")
    @classmethod
    def _forced_names_are_not_blank(cls, value: dict[str, bool]) -> dict[str, bool]:
        """Reject a blank parameter name.

        Args:
            value: The recorded per-parameter pinned values.

        Returns:
            The pinned values unchanged.

        Raises:
            ValueError: A key is blank or whitespace-only.
        """
        for param in value:
            if not param.strip():
                raise ValueError("param_forced key must not be blank")
        return value


class GovernanceConfig(BaseModel):
    """Complete governance configuration."""

    modes: dict[str, ModeDefinition] = Field(..., description="Mode definitions")
    transition_rules: dict[str, TransitionRule] = Field(
        default_factory=dict, description="Mode transition rules"
    )
    tools: dict[str, ToolPolicy] = Field(..., description="Tool policies")
    sub_agent_tools: dict[str, SubAgentToolDecision] = Field(
        default_factory=dict,
        description=(
            "The sub-agent principal's tool decisions, one per tool (FRE-1388, FRE-1463). "
            "Independent of `tools[name].allowed_in_modes`, which governs the primary only — "
            "a sub-agent is a distinct principal, not a subset of the primary's policy. "
            "Membership proves only that a decision was recorded; read "
            "`granted_sub_agent_tool_names()` for the grant set."
        ),
    )

    def granted_sub_agent_tool_names(self) -> tuple[str, ...]:
        """The tool names a sub-agent may actually use, in declaration order.

        The single accessor for the grant set (FRE-1463). Iterating
        ``sub_agent_tools`` directly yields every key, refusals included, which
        would grant a tool whose entry says ``granted: false``.

        Returns:
            The granted tool names, in the order the config declares them.
        """
        return tuple(name for name, decision in self.sub_agent_tools.items() if decision.granted)
