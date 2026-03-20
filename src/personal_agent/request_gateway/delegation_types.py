"""Stage B delegation types — structured handoff.

Machine-readable delegation packages that replace Stage A's markdown format.
Enables structured telemetry, outcome tracking, and pattern analysis.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 6.3
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class DelegationContext:
    """Context package for the external agent.

    Args:
        service_path: Primary code directory for the task.
        relevant_files: Specific files the agent should reference.
        conventions: Coding conventions to follow.
        db_schema: Database schema if relevant.
        test_patterns: Testing patterns to follow.
    """

    service_path: str
    relevant_files: list[str] | None = None
    conventions: list[str] | None = None
    db_schema: str | None = None
    test_patterns: str | None = None


@dataclass(frozen=True)
class DelegationPackage:
    """Structured instruction package for external agents.

    Args:
        task_id: Unique identifier for this delegation.
        target_agent: Agent to delegate to (e.g., "claude-code", "codex").
        task_description: What the external agent should do.
        context: Structured context about the codebase/project.
        created_at: When the package was created.
        memory_excerpt: Relevant memory items from Seshat.
        acceptance_criteria: How to verify the work is done.
        known_pitfalls: Lessons from past delegations.
        estimated_complexity: Complexity estimate (SIMPLE/MODERATE/COMPLEX).
    """

    task_id: str
    target_agent: str
    task_description: str
    context: DelegationContext
    created_at: datetime
    memory_excerpt: list[dict[str, str | float]] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    known_pitfalls: list[str] = field(default_factory=list)
    estimated_complexity: Literal["SIMPLE", "MODERATE", "COMPLEX"] = "MODERATE"
    # NOTE: Spec uses Sequence[MemoryItem] for memory_excerpt.
    # Slice 2 uses list[dict[str, str | float]] for simplicity.
    # TODO(Slice 3): Define MemoryItem type and migrate.


@dataclass(frozen=True)
class DelegationOutcome:
    """What comes back after delegation completes.

    Args:
        task_id: Matches the DelegationPackage task_id.
        success: Whether the delegation succeeded.
        rounds_needed: How many iterations were needed.
        what_worked: What went well (for learning).
        what_was_missing: What context was missing (for learning).
        artifacts_produced: Files/outputs created.
        duration_minutes: Total time spent.
        user_satisfaction: User rating 1-5 (None if not rated).
    """

    task_id: str
    success: bool
    rounds_needed: int
    what_worked: str
    what_was_missing: str
    artifacts_produced: list[str] = field(default_factory=list)
    duration_minutes: float = 0.0
    user_satisfaction: int | None = None
