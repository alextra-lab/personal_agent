"""Session-level narrative summariser (FRE-347 / FRE-346 G1).

Populates `SessionNode.session_summary` so cross-session recall returns a
short prose digest of each session, not just a list of dominant entities.

Design (defaults from FRE-347 ticket):

* Model role: ``captains_log_role`` (config/models.yaml) — same role used by
  reflection and insights. Defaults to ``gpt-5.4-nano`` (cheap, structured).
* Trigger: every consolidation pass for sessions that received new turns.
  Re-summarises on resume (MERGE in :Session overwrites the field).
* Budget: ``captains_log`` role; ``BudgetDenied`` → returns None, no exception.
  Consolidation never blocks on summarisation.
* Failure mode: returns None on any error path; the consolidator passes None
  through to ``SessionNode(session_summary=None)`` and the next consolidation
  pass retries.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.config import load_model_config
from personal_agent.config.settings import get_settings
from personal_agent.cost_gate import BudgetDenied
from personal_agent.llm_client import InferenceSlotTimeout, LLMTimeout, LocalLLMClient, ModelRole
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.trace import SystemTraceContext

log = get_logger(__name__)

_SUMMARY_SYSTEM_PROMPT = (
    "You write short, factual session digests for an agent's long-term memory. "
    "Output is read by the agent in future sessions to recall what happened. "
    "Be concrete: name specific topics, decisions, and actions. "
    "Do not use meta-language like 'in this session' or 'the user asked' — "
    "just state what happened. Output plain prose, no markdown, no JSON."
)

_SUMMARY_PROMPT_TEMPLATE = """\
Summarise this session in 2–4 sentences (target 200–500 characters total).

Session: {turn_count} turn(s) from {started} to {ended}.

Conversation excerpts (chronological):

{excerpts}

Return only the prose summary — no preamble, no quotes, no markdown.
"""

# Per-turn excerpt caps. Keep prompt under ~4k chars even for 20-turn sessions.
_MAX_TURNS_IN_PROMPT = 20
_USER_EXCERPT_CHARS = 200
_ASSISTANT_EXCERPT_CHARS = 200

# Output sanitisation bounds. The model may return slightly outside the target
# range; we accept up to 1000 chars and reject anything pathological.
_MIN_SUMMARY_CHARS = 20
_MAX_SUMMARY_CHARS = 1000


def _format_excerpt(capture: TaskCapture) -> str:
    """Format one capture as a compact two-line excerpt for the summarisation prompt."""
    user = (capture.user_message or "").strip().replace("\n", " ")[:_USER_EXCERPT_CHARS]
    assistant = (
        (capture.assistant_response or "").strip().replace("\n", " ")[:_ASSISTANT_EXCERPT_CHARS]
    )
    return f"User: {user}\nAssistant: {assistant}"


def _build_prompt(captures: list[TaskCapture]) -> str:
    """Build the summarisation prompt from an ordered list of captures."""
    started: datetime = captures[0].timestamp
    ended: datetime = captures[-1].timestamp
    turn_count = len(captures)
    sample = captures[:_MAX_TURNS_IN_PROMPT]
    excerpts = "\n\n".join(_format_excerpt(c) for c in sample)
    if turn_count > _MAX_TURNS_IN_PROMPT:
        excerpts += f"\n\n[...{turn_count - _MAX_TURNS_IN_PROMPT} more turn(s) omitted]"
    return _SUMMARY_PROMPT_TEMPLATE.format(
        turn_count=turn_count,
        started=started.isoformat(),
        ended=ended.isoformat(),
        excerpts=excerpts,
    )


def _sanitise(content: str) -> str | None:
    """Trim, validate length, return None if pathological."""
    text = (content or "").strip()
    # Strip surrounding quotes if the model wraps the summary
    if len(text) >= 2 and text[0] in {'"', "'"} and text[-1] == text[0]:
        text = text[1:-1].strip()
    if len(text) < _MIN_SUMMARY_CHARS:
        return None
    if len(text) > _MAX_SUMMARY_CHARS:
        text = text[:_MAX_SUMMARY_CHARS].rstrip()
    return text


async def generate_session_summary(
    captures: list[TaskCapture],
    *,
    session_id: str,
    trace_id: str = "consolidation",
) -> str | None:
    """Generate a short prose summary of a session's turns.

    Args:
        captures: Ordered list of TaskCapture (must be sorted by timestamp ASC).
        session_id: Session ID for structured logging.
        trace_id: Trace identifier for log correlation; defaults to "consolidation".

    Returns:
        A 200–1000 char prose summary, or None if generation failed for any reason
        (no captures, model error, budget denial, timeout, empty model output).
    """
    if not captures:
        return None

    settings = get_settings()
    if not getattr(settings, "session_summary_enabled", True):
        log.debug("session_summary_disabled_by_settings", session_id=session_id)
        return None

    model_config = load_model_config()
    role_name = model_config.captains_log_role
    model_def = model_config.models.get(role_name)
    provider = model_def.provider if model_def else None

    prompt = _build_prompt(captures)

    started_at = time.perf_counter()
    log.info(
        "session_summary_started",
        session_id=session_id,
        trace_id=trace_id,
        turn_count=len(captures),
        role=role_name,
        provider=provider,
        model_id=model_def.id if model_def else None,
    )

    try:
        if provider is not None:
            from personal_agent.llm_client.factory import get_llm_client

            cloud_client = get_llm_client(role_name=role_name)
            response: dict[str, Any] = await cloud_client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": prompt}],
                system_prompt=_SUMMARY_SYSTEM_PROMPT,
            )
            content = response.get("content", "") or ""
            model_used = model_def.id if model_def else role_name
        else:
            local_client = LocalLLMClient()
            model_role = ModelRole.from_str(role_name) or ModelRole.SUB_AGENT
            messages = [
                {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
            try:
                from personal_agent.llm_client.concurrency import InferencePriority

                llm_response = await local_client.respond(
                    role=model_role,
                    messages=messages,
                    system_prompt=None,
                    tools=None,
                    max_tokens=512,
                    max_retries=0,
                    timeout_s=60.0,
                    priority=InferencePriority.BACKGROUND,
                    priority_timeout=60.0,
                    trace_ctx=SystemTraceContext.new("session_summary"),
                )
            except (LLMTimeout, InferenceSlotTimeout) as e:
                log.warning(
                    "session_summary_local_timeout",
                    session_id=session_id,
                    trace_id=trace_id,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                return None
            content = llm_response.get("content", "") or ""
            model_used = role_name
    except BudgetDenied as e:
        log.warning(
            "session_summary_budget_denied",
            session_id=session_id,
            trace_id=trace_id,
            denial_reason=e.denial_reason,
            role=e.role,
            cap=str(e.cap),
            spend=str(e.current_spend),
        )
        return None
    except Exception as e:  # noqa: BLE001 — never block consolidation on summary errors
        log.warning(
            "session_summary_failed",
            session_id=session_id,
            trace_id=trace_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return None

    summary = _sanitise(content)
    duration_ms = (time.perf_counter() - started_at) * 1000.0

    if summary is None:
        log.warning(
            "session_summary_empty_or_too_short",
            session_id=session_id,
            trace_id=trace_id,
            content_len=len(content),
            duration_ms=duration_ms,
            model_used=model_used,
        )
        return None

    log.info(
        "session_summary_generated",
        session_id=session_id,
        trace_id=trace_id,
        turn_count=len(captures),
        char_count=len(summary),
        duration_ms=duration_ms,
        model_used=model_used,
    )
    return summary
