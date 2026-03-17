"""Stage A: Delegation Instruction Composition.

Produces structured markdown delegation packages that the user
can copy to external agents (Claude Code, Codex, etc.).

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 6.2
"""

from __future__ import annotations

from collections.abc import Sequence


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
