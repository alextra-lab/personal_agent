"""Delegation Instruction Composition — Stage A (markdown) + Stage B (structured).

Stage A: produces markdown packages for copy-paste to external agents.
Stage B: produces machine-readable DelegationPackage objects with telemetry.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Sections 6.2, 6.3
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Literal

import structlog

from personal_agent.request_gateway.delegation_types import (
    DelegationContext,
    DelegationOutcome,
    DelegationPackage,
)

logger = structlog.get_logger(__name__)


def compose_delegation_instructions(
    task_description: str,
    context_notes: Sequence[str] | None = None,
    conventions: Sequence[str] | None = None,
    acceptance_criteria: Sequence[str] | None = None,
    known_pitfalls: Sequence[str] | None = None,
) -> str:
    """Compose a markdown delegation instruction package.

    Args:
        task_description: What the external agent should do.
        context_notes: Relevant context about the codebase/project.
        conventions: Coding conventions to follow.
        acceptance_criteria: How to verify the work is done.
        known_pitfalls: Lessons from past delegations.

    Returns:
        Formatted markdown string ready for copy-paste.
    """
    sections: list[str] = []

    sections.append(f"# Delegation Instruction Package\n\n## Task\n\n{task_description}")

    if context_notes:
        items = "\n".join(f"- {note}" for note in context_notes)
        sections.append(f"## Context\n\n{items}")

    if conventions:
        items = "\n".join(f"- {conv}" for conv in conventions)
        sections.append(f"## Conventions\n\n{items}")

    if acceptance_criteria:
        items = "\n".join(f"- [ ] {criterion}" for criterion in acceptance_criteria)
        sections.append(f"## Acceptance Criteria\n\n{items}")

    if known_pitfalls:
        items = "\n".join(f"- {pitfall}" for pitfall in known_pitfalls)
        sections.append(f"## Known Pitfalls (from memory)\n\n{items}")

    return "\n\n".join(sections) + "\n"


def compose_delegation_package(
    task_description: str,
    trace_id: str,
    target_agent: str = "claude-code",
    service_path: str = "src/personal_agent/",
    relevant_files: list[str] | None = None,
    conventions: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    known_pitfalls: list[str] | None = None,
    memory_excerpt: list[dict[str, str | float]] | None = None,
    estimated_complexity: Literal["SIMPLE", "MODERATE", "COMPLEX"] = "MODERATE",
) -> DelegationPackage:
    """Compose a Stage B structured delegation package.

    Args:
        task_description: What the external agent should do.
        trace_id: Request trace identifier.
        target_agent: Agent to delegate to.
        service_path: Primary code directory.
        relevant_files: Files the agent should reference.
        conventions: Coding conventions.
        acceptance_criteria: Verification criteria.
        known_pitfalls: Lessons from past delegations.
        memory_excerpt: Relevant memory items from Seshat.
        estimated_complexity: Task complexity estimate.

    Returns:
        DelegationPackage ready for handoff.
    """
    task_id = f"del-{uuid.uuid4().hex[:12]}"

    package = DelegationPackage(
        task_id=task_id,
        target_agent=target_agent,
        task_description=task_description,
        context=DelegationContext(
            service_path=service_path,
            relevant_files=relevant_files,
            conventions=conventions,
        ),
        memory_excerpt=memory_excerpt or [],
        acceptance_criteria=acceptance_criteria or [],
        known_pitfalls=known_pitfalls or [],
        estimated_complexity=estimated_complexity,
        created_at=datetime.now(tz=timezone.utc),
    )

    logger.info(
        "delegation_package_created",
        task_id=task_id,
        target_agent=target_agent,
        context_items=len(relevant_files or []),
        memory_items=len(package.memory_excerpt),
        criteria_count=len(package.acceptance_criteria),
        pitfall_count=len(package.known_pitfalls),
        complexity=estimated_complexity,
        trace_id=trace_id,
    )

    return package


def record_delegation_outcome(
    outcome: DelegationOutcome,
    trace_id: str,
) -> None:
    """Log a delegation outcome for telemetry and insights analysis.

    NOTE: ES persistence is via structlog → Elasticsearch pipeline
    (configured in telemetry module). No explicit ES indexing needed here.
    The structlog.info call is automatically routed to ES by the existing
    telemetry infrastructure.

    Args:
        outcome: The completed delegation outcome.
        trace_id: Request trace identifier.
    """
    logger.info(
        "delegation_outcome_recorded",
        task_id=outcome.task_id,
        success=outcome.success,
        rounds_needed=outcome.rounds_needed,
        what_worked=outcome.what_worked,
        what_was_missing=outcome.what_was_missing,
        artifacts_count=len(outcome.artifacts_produced),
        duration_minutes=outcome.duration_minutes,
        user_satisfaction=outcome.user_satisfaction,
        trace_id=trace_id,
    )
