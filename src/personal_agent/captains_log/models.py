"""Pydantic models for Captain's Log entries.

These models define the structure of Captain's Log entries as documented
in ../../docs/architecture_decisions/captains_log/README.md.

Extended by ADR-0030: Categorization, dedup fingerprinting, and Linear promotion fields.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ChangeCategory(str, Enum):
    """Taxonomy of improvement types (ADR-0030).

    Classifies proposed changes for dedup grouping and dashboard filtering.
    """

    PERFORMANCE = "performance"
    RELIABILITY = "reliability"
    CONCURRENCY = "concurrency"
    KNOWLEDGE_QUALITY = "knowledge"
    COST = "cost"
    UX = "ux"
    OBSERVABILITY = "observability"
    ARCHITECTURE = "architecture"
    SAFETY = "safety"


class ChangeScope(str, Enum):
    """Target subsystem for a proposed change (ADR-0030).

    Combined with ChangeCategory to form the dedup fingerprint namespace.
    """

    LLM_CLIENT = "llm_client"
    ORCHESTRATOR = "orchestrator"
    SECOND_BRAIN = "second_brain"
    CAPTAINS_LOG = "captains_log"
    BRAINSTEM = "brainstem"
    TOOLS = "tools"
    TELEMETRY = "telemetry"
    GOVERNANCE = "governance"
    INSIGHTS = "insights"
    CONFIG = "config"
    CROSS_CUTTING = "cross_cutting"


class Metric(BaseModel):
    """Structured metric with typed value and optional unit.

    Used for programmatic analysis of Captain's Log metrics (ADR-0014).
    Enables time-series analysis, anomaly detection, and cross-request aggregation
    without fragile string parsing.

    Examples:
        >>> Metric(name="cpu_percent", value=9.3, unit="%")
        >>> Metric(name="duration_seconds", value=5.4, unit="s")
        >>> Metric(name="llm_calls", value=2, unit=None)
    """

    name: str = Field(
        ...,
        description="Metric identifier (e.g., 'cpu_percent', 'duration_seconds', 'llm_calls')",
    )
    value: float | int | str = Field(..., description="Metric value (prefer numbers when possible)")
    unit: str | None = Field(None, description="Unit of measurement (e.g., '%', 's', 'ms', 'MB')")

    class Config:
        """Pydantic model configuration."""

        json_schema_extra = {
            "examples": [
                {"name": "cpu_percent", "value": 9.3, "unit": "%"},
                {"name": "duration_seconds", "value": 5.4, "unit": "s"},
                {"name": "llm_calls", "value": 2, "unit": None},
                {"name": "memory_percent", "value": 53.4, "unit": "%"},
                {"name": "gpu_percent", "value": 3.2, "unit": "%"},
            ]
        }


class CaptainLogEntryType(str, Enum):
    """Types of Captain's Log entries."""

    REFLECTION = "reflection"
    CONFIG_PROPOSAL = "config_proposal"
    HYPOTHESIS = "hypothesis"
    OBSERVATION = "observation"
    IDEA = "idea"


class CaptainLogStatus(str, Enum):
    """Status of a Captain's Log entry."""

    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    IMPLEMENTED = "implemented"


class ProposedChange(BaseModel):
    """Proposed improvement or change.

    Extended by ADR-0030 with category/scope for dedup and a merge counter.
    All new fields are optional for backward compatibility with existing entries.
    """

    what: str = Field(..., description="What to change")
    why: str = Field(..., description="Why it would help")
    how: str = Field(..., description="How to implement it")
    category: ChangeCategory | None = Field(None, description="Improvement category (ADR-0030)")
    scope: ChangeScope | None = Field(None, description="Target subsystem (ADR-0030)")
    fingerprint: str | None = Field(
        None, description="Semantic dedup key: sha256(category:scope:normalized_what)[:16]"
    )
    seen_count: int = Field(
        default=1, ge=1, description="How many times this proposal has been observed"
    )
    first_seen: datetime | None = Field(None, description="Timestamp of the earliest observation")
    related_entry_ids: list[str] = Field(
        default_factory=list,
        description="Entry IDs that were merged into this proposal",
    )


class TelemetryRef(BaseModel):
    """Reference to telemetry trace or metric."""

    trace_id: str | None = Field(None, description="Trace ID for execution trace")
    metric_name: str | None = Field(None, description="Metric name")
    value: Any | None = Field(None, description="Metric value")


class CaptainLogEntry(BaseModel):
    """Captain's Log entry model.

    Represents a structured entry in the Captain's Log for agent
    self-reflection, observations, and improvement proposals.
    """

    entry_id: str = Field(..., description="Unique entry ID (e.g., 'CL-2025-12-28-001')")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Entry timestamp (UTC)",
    )
    type: CaptainLogEntryType = Field(..., description="Entry type")
    title: str = Field(..., description="Short, actionable title")
    rationale: str = Field(..., description="Multi-line explanation of why this entry exists")

    # Optional fields based on entry type
    proposed_change: ProposedChange | None = Field(
        None, description="Proposed change (for config_proposal type)"
    )
    supporting_metrics: list[str] = Field(
        default_factory=list, description="Human-readable metrics (e.g., 'cpu: 9.3%')"
    )
    metrics_structured: list[Metric] | None = Field(
        None,
        description="Structured metrics for programmatic analysis (ADR-0014). "
        "Optional field for backward compatibility. When present, enables "
        "time-series analysis, anomaly detection, and cross-request aggregation.",
    )
    impact_assessment: str | None = Field(None, description="Expected impact assessment")
    status: CaptainLogStatus = Field(
        default=CaptainLogStatus.AWAITING_APPROVAL, description="Entry status"
    )
    reviewer_notes: str | None = Field(None, description="Notes from project owner review")
    related_adrs: list[str] = Field(default_factory=list, description="Related ADR references")
    related_experiments: list[str] = Field(
        default_factory=list, description="Related experiment references"
    )
    telemetry_refs: list[TelemetryRef] = Field(
        default_factory=list, description="References to telemetry traces/metrics"
    )

    # ADR-0030: Linear promotion tracking
    linear_issue_id: str | None = Field(
        None,
        description="Linear issue ID if this proposal was promoted to backlog (ADR-0030)",
    )

    # Type-specific optional fields
    experiment_design: list[str] | None = Field(
        None, description="Experiment design (for hypothesis type)"
    )
    expected_outcome: str | None = Field(None, description="Expected outcome (for hypothesis type)")
    potential_implementation: list[str] | None = Field(
        None, description="Potential implementation (for idea type)"
    )

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_timestamp(cls, v: Any) -> datetime:
        """Parse timestamp from string or datetime."""
        if isinstance(v, str):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        if isinstance(v, datetime):
            return v
        raise ValueError(f"Invalid timestamp: {v}")

    @field_validator("timestamp", mode="after")
    @classmethod
    def ensure_utc(cls, v: datetime) -> datetime:
        """Ensure timestamp is timezone-aware (UTC)."""
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    def model_dump_json_pretty(self) -> str:
        """Export entry as pretty-printed JSON string.

        Returns:
            JSON string with 2-space indentation.
        """
        return self.model_dump_json(indent=2)
