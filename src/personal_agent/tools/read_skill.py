"""read_skill tool — load a skill doc body on demand.

Phase B (FRE-skill-routing): model-decided routing.  The compact skill index
injected into the system prompt tells the model which skills exist; when the
model decides a skill is relevant it calls ``read_skill(name=...)`` to fetch
the full body.

The tool itself is stateless — dedup tracking (``loaded_skills``) is handled
in ``_dispatch_tool_call`` in the executor, not here.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog

from personal_agent.telemetry import TraceContext
from personal_agent.tools.contract import ToolError, error_codes
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = structlog.get_logger(__name__)

ReadSkillCode = Literal["missing_name", "unknown_skill"]


class ReadSkillError(ToolError[ReadSkillCode]):
    """A ``read_skill`` failure (ADR-0156 D6.1)."""


# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------

read_skill_tool = ToolDefinition(
    name="read_skill",
    description=(
        "Load the full guidance of one skill by its name and return the skill body as text. "
        "Use it when the skill index lists a skill that fits the current task, before you "
        "start that task. "
        "Do not use it for a skill already loaded in this conversation, or to read other "
        "files (use read). "
        "The name must match the index exactly; an unknown name returns the list of names. "
        'Example: read_skill(name="query-elasticsearch").'
    ),
    category="read_only",
    parameters=[
        ToolParameter(
            name="name",
            type="string",
            description="Skill name exactly as listed in the skill index (e.g. 'query-elasticsearch')",
            required=True,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=5,
    rate_limit_per_hour=None,
    error_codes=error_codes(ReadSkillError),
)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def read_skill_executor(
    name: str = "",
    session_id: str | None = None,
    *,
    ctx: TraceContext,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Return the full body of a skill doc by name.

    Args:
        name: Skill name to look up (must match a name in the skill index).
            If empty, returns the list of available skill names instead of crashing.
        session_id: Optional session identifier for logging.
        ctx: Telemetry trace context (passed as 'ctx' by the tool executor layer).

    Returns:
        Dict with ``skill_name`` and ``body``.

    Raises:
        ReadSkillError: ``missing_name`` or ``unknown_skill``; the message lists the names.
    """
    from personal_agent.orchestrator.skills import get_all_skills  # noqa: PLC0415

    all_skills = get_all_skills()
    known = sorted(all_skills.keys())

    if not name:
        raise ReadSkillError(
            "missing_name",
            f"name is required. Available skills: {', '.join(known)}",
            retryable=True,
            next_action="Call read_skill again with one of the listed names.",
        )

    skill = all_skills.get(name)

    if skill is None:
        log.warning(
            "missing_skill_requested",
            trace_id=ctx.trace_id,
            session_id=session_id,
            requested_name=name,
            available_skills=known,
            known_count=len(known),
        )
        raise ReadSkillError(
            "unknown_skill",
            f"Unknown skill '{name}'. Available skills: {', '.join(known)}",
            retryable=False,
            next_action="Call read_skill with one of the listed names, or go on without it.",
        )

    log.info(
        "read_skill_invoked",
        skill_name=name,
        body_chars=len(skill.body),
        session_id=session_id,
        trace_id=ctx.trace_id,
    )
    return {
        "skill_name": name,
        "body": skill.body,
    }
