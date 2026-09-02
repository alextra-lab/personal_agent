"""Pydantic models for Captain's Log entries.

These models define the structure of Captain's Log entries as documented
in ../../docs/architecture_decisions/captains_log/README.md.

Extended by ADR-0030: Categorization, dedup fingerprinting, and Linear promotion fields.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, assert_never

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


class ProposalSource(str, Enum):
    """Producer that generated a ProposedChange (ADR-0105 D1).

    The self-improvement pipeline has one promotion path fed by multiple
    producers; this discriminator makes the origin queryable without a
    second pipeline. Extensible — a new producer adds a member here rather
    than a competing promotion path. Adding a member also requires a new
    ``case`` in :func:`producer_dimension` (ADR-0125 D1) — see that
    function's docstring.
    """

    STATISTICAL_DETECTOR = "statistical_detector"
    REFLECTION = "reflection"
    # ADR-0125 D1: migration-only sentinel for a stored entry whose original
    # source predates this field and cannot be recovered. No producer may
    # construct a ProposedChange with this value — only
    # scripts/migrate_fre1001_captains_log_source_backfill.py emits it, and
    # tests/test_captains_log/test_models_adr_0105.py's
    # test_legacy_unattributable_never_used_by_a_real_producer enforces that.
    LEGACY_UNATTRIBUTABLE = "legacy_unattributable"


class Dimension(str, Enum):
    """The two quality dimensions a producer's output serves (ADR-0125 D1).

    Dimension is a property of the *producer*, not of the subject matter —
    a dimension-1 producer may reason about anything, but its output may
    never enter user-facing context (ADR-0125 D2).
    """

    HARNESS_HEALTH = "dimension_1_harness_health"
    OUTPUT_QUALITY = "dimension_2_output_quality"


def producer_dimension(source: ProposalSource) -> Dimension:
    """Map a producer to the exactly-one dimension it serves (ADR-0125 D1).

    Total over the ``ProposalSource`` vocabulary and build-enforced: the
    ``case _: assert_never(source)`` fallback makes an unmapped member fail
    both statically (mypy rejects passing a non-``Never``-narrowed value to
    ``assert_never`` — verified against this repo's ``strict = true`` mypy
    config) and at runtime (``assert_never`` raises ``AssertionError``),
    so a new producer can never silently fall through to a runtime default.

    ``LEGACY_UNATTRIBUTABLE`` maps to ``HARNESS_HEALTH`` as a conservative
    quarantine classification, not a claim of recovered provenance — a
    dimension-1 producer's output can never reach user-facing context
    (ADR-0125 D2), so treating un-attributable legacy material as
    dimension-1 is the safe default regardless of what actually produced it.

    Args:
        source: The producer discriminator.

    Returns:
        The dimension that producer's output belongs to.
    """
    match source:
        case ProposalSource.STATISTICAL_DETECTOR:
            return Dimension.HARNESS_HEALTH
        case ProposalSource.REFLECTION:
            return Dimension.HARNESS_HEALTH
        case ProposalSource.LEGACY_UNATTRIBUTABLE:
            return Dimension.HARNESS_HEALTH
        case _:
            assert_never(source)


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
    Most later fields are optional for backward compatibility with existing
    entries — ``source`` is the exception (ADR-0125 D1): it is required, so a
    stored payload written before ADR-0105 introduced it no longer validates
    as-is. See ``scripts/migrate_fre1001_captains_log_source_backfill.py``.
    """

    what: str = Field(..., description="What to change")
    why: str = Field(..., description="Why it would help")
    how: str = Field(..., description="How to implement it")
    category: ChangeCategory | None = Field(None, description="Improvement category (ADR-0030)")
    scope: ChangeScope | None = Field(None, description="Target subsystem (ADR-0030)")
    source: ProposalSource = Field(
        ...,
        description=(
            "Producer that generated this proposal (ADR-0105 D1). Non-nullable "
            "(ADR-0125 D1) — a write that omits it is rejected, not defaulted. "
            "Every current producer sets it explicitly; a stored entry that "
            "predates this field must be migrated (ProposalSource."
            "LEGACY_UNATTRIBUTABLE) before it validates."
        ),
    )
    fingerprint: str | None = Field(
        None,
        description=(
            "Canonical proposal identity. Computed as "
            "sha256(category:scope:normalized_what)[:16] at first sighting, then "
            "carried forward onto every later sighting absorbed into the same "
            "(source, category, scope) group (FRE-1354) — so it is the group's "
            "stable identity, not necessarily a hash of THIS entry's `what`. "
            "Consumers treat it as opaque and must not recompute-and-compare it. "
            "Carrying it forward is what maps repeated sightings of one idea to a "
            "single Linear ticket; on 2026-06-26 six sightings hashed six different "
            "ways and produced six tickets."
        ),
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
    # FRE-523: eval-derived reflection entries are written (the cognitive pipeline
    # runs during eval) but must never be promoted to Linear — the promotion
    # pipeline skips entries with eval_mode=True.
    eval_mode: bool = Field(
        default=False, description="True when the entry originated from an eval run (FRE-523)"
    )
    # FRE-1340: durable landing spot for the FRE-328/FRE-1321 gap-recognition signal.
    # Reflection previously only fired a fire-and-forget log.warning for a detected
    # missing-skill request, which never reached the persisted, reachable entry.
    missing_skill_names: list[str] = Field(
        default_factory=list,
        description=(
            "Skills requested by name during this turn that don't exist in the skill "
            "library (FRE-328/FRE-1321 gap-recognition signal)."
        ),
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
