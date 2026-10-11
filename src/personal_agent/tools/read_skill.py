"""read_skill tool — load a skill's instructions, or one file of its folder (ADR-0156 D3).

The skill catalog in the system prompt lists each skill's name and description (D2). When a
task matches a skill, the model calls ``read_skill(name=...)``: it returns the ``SKILL.md`` body
and the list of the skill's other files, without reading them (S2). ``read_skill(name, path)``
returns one listed file. Any other path is refused.

The tool reads no private data and needs no filesystem access, so it works for the primary and
for every worker type (D3.3). Dedup of a repeated body read happens in ``dispatch_tool_call``.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog

from personal_agent.orchestrator.skills import (
    SkillFileRefused,
    get_all_skills,
    list_skill_files,
    read_skill_file,
    skill_names,
)
from personal_agent.telemetry import TraceContext
from personal_agent.tools.contract import ToolError, error_codes
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = structlog.get_logger(__name__)

ReadSkillCode = Literal["missing_name", "unknown_skill", "invalid_path"]


class ReadSkillError(ToolError[ReadSkillCode]):
    """A ``read_skill`` failure (ADR-0156 D6.1)."""


# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------


def build_read_skill_tool() -> ToolDefinition:
    """Build the ``read_skill`` definition, with ``name`` as an enum of the skill names (D2.2).

    Returns:
        The tool definition.
    """
    names = list(skill_names())
    return ToolDefinition(
        name="read_skill",
        description=(
            "Load the instructions of one skill from the skill catalog in the system prompt. "
            "Use it when a task matches a skill's description, before you start that task, "
            "and follow the instructions it returns. "
            "Without a path it returns the skill body and the list of the skill's files; with "
            "a path it returns that one listed file, for example a reference or a script. "
            "Do not use it to read any other file (use read), or to load a skill again in the "
            "same turn. "
            'Examples: read_skill(name="query-elasticsearch"), '
            'read_skill(name="query-tempo", path="references/<file>.md").'
        ),
        category="read_only",
        parameters=[
            ToolParameter(
                name="name",
                type="string",
                description="The skill name, exactly as the catalog lists it.",
                required=True,
                default=None,
                json_schema={
                    "type": "string",
                    "enum": names,
                    "description": "The skill name, exactly as the catalog lists it.",
                },
            ),
            ToolParameter(
                name="path",
                type="string",
                description=(
                    "Optional. One file of the skill, exactly as the file list of an earlier "
                    "read_skill call gives it (for example 'references/guide.md')."
                ),
                required=False,
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


read_skill_tool = build_read_skill_tool()


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def read_skill_executor(
    name: str = "",
    path: str | None = None,
    session_id: str | None = None,
    *,
    ctx: TraceContext,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Return a skill body and its file list, or one listed file of the skill.

    Args:
        name: Skill name, one of the catalog names.
        path: Optional relative path of one listed file of the skill.
        session_id: Optional session identifier for logging.
        ctx: Telemetry trace context (passed as 'ctx' by the tool executor layer).

    Returns:
        Without ``path``: ``skill_name``, ``base_dir``, ``body`` and ``files``. With ``path``:
        ``skill_name``, ``path`` and ``content``.

    Raises:
        ReadSkillError: ``missing_name``, ``unknown_skill`` or ``invalid_path``.
    """
    all_skills = get_all_skills()
    known = sorted(all_skills)

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

    files = [f.path for f in list_skill_files(skill.folder)]
    if path:
        try:
            content = read_skill_file(skill.folder, path)
        except SkillFileRefused as exc:
            log.warning(
                "read_skill_path_refused",
                trace_id=ctx.trace_id,
                session_id=session_id,
                skill_name=name,
                requested_path=path,
                reason=str(exc),
            )
            listed = ", ".join(files) or "none"
            raise ReadSkillError(
                "invalid_path",
                f"{exc} Files of skill '{name}': {listed}",
                retryable=True,
                next_action=(
                    "Call read_skill with a path from the list exactly, or without a path."
                ),
            ) from exc
        log.info(
            "read_skill_invoked",
            skill_name=name,
            path=path,
            content_chars=len(content),
            session_id=session_id,
            trace_id=ctx.trace_id,
        )
        return {"skill_name": name, "path": path, "content": content}

    log.info(
        "read_skill_invoked",
        skill_name=name,
        path=None,
        body_chars=len(skill.body),
        file_count=len(files),
        session_id=session_id,
        trace_id=ctx.trace_id,
    )
    return {
        "skill_name": name,
        "base_dir": str(skill.folder),
        "body": skill.body,
        "files": files,
    }
