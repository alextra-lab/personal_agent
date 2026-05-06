"""read_skill tool — load a skill doc body on demand.

Phase B (FRE-skill-routing): model-decided routing.  The compact skill index
injected into the system prompt tells the model which skills exist; when the
model decides a skill is relevant it calls ``read_skill(name=...)`` to fetch
the full body.

The tool itself is stateless — dedup tracking (``loaded_skills``) is handled
in ``_dispatch_tool_call`` in the executor, not here.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from personal_agent.telemetry import TraceContext
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------

read_skill_tool = ToolDefinition(
    name="read_skill",
    description=(
        "Load the full guidance for a skill by name. "
        "Use when the skill index mentions a skill relevant to your current task. "
        "Returns the complete skill doc body as text."
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
)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def read_skill_executor(
    name: str,
    trace_ctx: TraceContext,
    session_id: str | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Return the full body of a skill doc by name.

    Args:
        name: Skill name to look up (must match a name in the skill index).
        trace_ctx: Telemetry trace context.
        session_id: Optional session identifier for logging.

    Returns:
        Dict with ``skill_name``, ``body``, and ``status``.
        On unknown name, returns ``status: "error"`` with an explanatory ``hint``.
    """
    from personal_agent.orchestrator.skills import get_all_skills  # noqa: PLC0415

    all_skills = get_all_skills()
    skill = all_skills.get(name)

    if skill is None:
        known = sorted(all_skills.keys())
        hint = f"Unknown skill '{name}'. Available skills: {', '.join(known)}"
        log.warning(
            "read_skill_unknown_name",
            skill_name=name,
            known_count=len(known),
        )
        return {"status": "error", "hint": hint}

    log.info(
        "read_skill_invoked",
        skill_name=name,
        body_chars=len(skill.body),
        session_id=session_id,
    )
    return {
        "status": "ok",
        "skill_name": name,
        "body": skill.body,
    }
