"""Pydantic models for governance configuration.

This module defines the schema for governance policies including:
- Operational modes (NORMAL, ALERT, DEGRADED, LOCKDOWN, RECOVERY)
- Tool permissions and risk classifications
- Model constraints per mode
- Safety policies (content filtering, rate limits)
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Mode(str, Enum):
    """Operational modes for the agent."""

    NORMAL = "NORMAL"
    ALERT = "ALERT"
    DEGRADED = "DEGRADED"
    LOCKDOWN = "LOCKDOWN"
    RECOVERY = "RECOVERY"


class ModeThresholds(BaseModel):
    """Thresholds that trigger mode transitions."""

    cpu_load_percent: float | None = Field(None, ge=0.0, le=100.0, description="CPU load threshold")
    memory_used_percent: float | None = Field(
        None, ge=0.0, le=100.0, description="Memory usage threshold"
    )
    tool_error_rate: float | None = Field(
        None, ge=0.0, le=1.0, description="Tool error rate threshold (0-1)"
    )
    policy_violations_per_10min: int | None = Field(
        None, ge=0, description="Policy violations per 10 minutes"
    )
    repeated_high_risk_calls: int | None = Field(
        None, ge=0, description="Repeated high-risk tool calls threshold"
    )


class ModeDefinition(BaseModel):
    """Definition of an operational mode."""

    description: str = Field(..., description="Human-readable description of the mode")
    max_concurrent_tasks: int = Field(..., ge=0, description="Maximum concurrent tasks allowed")
    background_monitoring_enabled: bool = Field(
        ..., description="Whether background monitoring is enabled"
    )
    allowed_tool_categories: list[str] = Field(
        default_factory=list, description="Tool categories allowed in this mode"
    )
    require_approval_for: list[str] = Field(
        default_factory=list,
        description="List of actions/categories requiring human approval",
    )
    thresholds: ModeThresholds = Field(
        default_factory=ModeThresholds, description="Mode transition thresholds"
    )


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


class ToolCategory(BaseModel):
    """Category of tools with shared risk characteristics."""

    description: str = Field(..., description="Description of the tool category")
    risk_level: str = Field(..., description="Risk level: low, medium, high")
    requires_approval_in_modes: list[str] = Field(
        default_factory=list, description="Modes requiring approval for this category"
    )
    requires_outbound_gateway: bool = Field(
        False, description="Whether outbound gateway is required"
    )
    examples: list[str] = Field(default_factory=list, description="Example tool names")


class ToolPolicy(BaseModel):
    """Policy for a specific tool."""

    category: str = Field(..., description="Tool category")
    allowed_in_modes: list[str] = Field(..., description="Modes where tool is allowed")
    requires_approval_in_modes: list[str] = Field(
        default_factory=list, description="Modes requiring approval"
    )
    forbidden_in_modes: list[str] = Field(
        default_factory=list, description="Modes where tool is forbidden"
    )
    requires_approval: bool = Field(False, description="Whether tool always requires approval")
    max_file_size_mb: int | None = Field(
        None, ge=0, description="Maximum file size in MB (for file tools)"
    )
    allowed_paths: list[str] = Field(default_factory=list, description="Allowed path patterns")
    forbidden_paths: list[str] = Field(default_factory=list, description="Forbidden path patterns")
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


class ModelRoleConstraints(BaseModel):
    """Constraints for a specific model role."""

    max_tokens: int = Field(..., ge=1, description="Maximum tokens")
    temperature: float = Field(..., ge=0.0, le=2.0, description="Temperature setting")
    timeout_seconds: int = Field(..., ge=1, description="Timeout in seconds")


class ModeModelConstraints(BaseModel):
    """Model constraints for a specific mode."""

    allowed_roles: list[str] = Field(..., description="Allowed model roles")
    max_tokens: dict[str, int] = Field(default_factory=dict, description="Max tokens per role")
    temperature: dict[str, float] = Field(default_factory=dict, description="Temperature per role")
    timeout_seconds: dict[str, int] = Field(default_factory=dict, description="Timeout per role")


class SecretPattern(BaseModel):
    """Pattern for detecting secrets in content."""

    regex: str = Field(..., description="Regex pattern to match")
    action: str = Field(..., description="Action: block, warn, or redact")
    redaction: str | None = Field(None, description="Redaction text if action is redact")


class ContentFiltering(BaseModel):
    """Content filtering configuration."""

    enabled: bool = Field(True, description="Whether content filtering is enabled")
    secret_patterns: list[SecretPattern] = Field(
        default_factory=list, description="Patterns for detecting secrets"
    )
    forbidden_content_patterns: list[dict[str, Any]] = Field(
        default_factory=list, description="Forbidden content patterns"
    )


class OutboundGateway(BaseModel):
    """Outbound gateway configuration."""

    enabled: bool = Field(True, description="Whether outbound gateway is enabled")
    allowed_domains: list[str] = Field(default_factory=list, description="Allowed domain patterns")
    blocked_domains: list[str] = Field(default_factory=list, description="Blocked domain patterns")
    require_approval_for_new_domains: bool = Field(
        True, description="Whether new domains require approval"
    )
    max_request_size_kb: int = Field(1024, ge=1, description="Maximum request size in KB")


class RateLimits(BaseModel):
    """Rate limits per mode."""

    tool_calls_per_minute: int = Field(..., ge=0, description="Tool calls per minute")
    llm_calls_per_minute: int = Field(..., ge=0, description="LLM calls per minute")
    outbound_requests_per_hour: int = Field(..., ge=0, description="Outbound requests per hour")


class HumanApprovalRule(BaseModel):
    """Rule for when human approval is required."""

    category: str | None = Field(None, description="Tool category")
    risk_level: str | None = Field(None, description="Risk level")
    modes: list[str] = Field(..., description="Modes where approval is required")


class HumanApproval(BaseModel):
    """Human approval configuration."""

    timeout_seconds: int = Field(300, ge=1, description="Approval request timeout")
    require_approval_for: list[HumanApprovalRule] = Field(
        default_factory=list, description="Rules for when approval is required"
    )


class SafetyConfig(BaseModel):
    """Safety and security configuration."""

    content_filtering: ContentFiltering = Field(
        default_factory=ContentFiltering, description="Content filtering settings"
    )
    outbound_gateway: OutboundGateway = Field(
        default_factory=OutboundGateway, description="Outbound gateway settings"
    )
    rate_limits: dict[str, RateLimits] = Field(
        default_factory=dict, description="Rate limits per mode"
    )
    human_approval: HumanApproval = Field(
        default_factory=HumanApproval, description="Human approval settings"
    )


class GovernanceConfig(BaseModel):
    """Complete governance configuration."""

    modes: dict[str, ModeDefinition] = Field(..., description="Mode definitions")
    transition_rules: dict[str, TransitionRule] = Field(
        default_factory=dict, description="Mode transition rules"
    )
    tool_categories: dict[str, ToolCategory] = Field(
        default_factory=dict, description="Tool category definitions"
    )
    tools: dict[str, ToolPolicy] = Field(..., description="Tool policies")
    mode_constraints: dict[str, ModeModelConstraints] = Field(
        ..., description="Model constraints per mode"
    )
    safety: SafetyConfig = Field(default_factory=SafetyConfig, description="Safety config")
