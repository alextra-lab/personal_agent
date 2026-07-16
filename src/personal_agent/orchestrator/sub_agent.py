"""Sub-agent runner — executes focused inference calls.

Each sub-agent is a single LLM call with a constrained context slice.
The runner acquires a concurrency slot, runs the inference, and
returns a SubAgentResult with a compressed summary.

Full output goes to ES via structlog; only the summary enters
the primary agent's synthesis context.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.6
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import structlog

from personal_agent.captains_log.capture import SubAgentCapture, write_sub_agent_capture
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec

logger = structlog.get_logger(__name__)


# Marker the primary injects when proactive-memory/KG entities are in context
# (executor._render_memory_section). Scanned in sub-agent context to answer the
# question FRE-505 exists for: "was memory/KG in the sub-agent's input?"
_MEMORY_CONTEXT_MARKER = "## Your Memory Graph"
# Per-message content preview length (mirrors executor.llm_call_messages_debug).
_CONTEXT_PREVIEW_CHARS = 200

# System prompt for sub-agents: focused, no personality
_SUB_AGENT_SYSTEM_PROMPT = (
    "You are a focused sub-agent executing a specific sub-task. "
    "Be concise and direct. Respond with the requested output format only. "
    "Do not ask follow-up questions. Do not add preamble or explanation "
    "beyond what was requested."
)


def _extract_call_cost(response: Any) -> float:
    """Pull the per-call ``cost_usd`` from an LLM response.

    ``llm_client.respond`` returns an ``LLMResponse`` mapping carrying
    ``cost_usd`` on paid/cloud calls (``NotRequired``); the PARALLEL_INFERENCE
    path and some tests return a bare string. Both are handled (FRE-501).

    Args:
        response: The value returned by ``llm_client.respond``.

    Returns:
        The call cost in USD, or 0.0 when absent or the response is a bare string.
    """
    if isinstance(response, Mapping):
        return float(response.get("cost_usd") or 0.0)
    return 0.0


def _summarize_input_context(system_content: str, spec: SubAgentSpec) -> dict[str, Any]:
    """Build the structured input-context breakdown for an audit record (FRE-505).

    Answers "what was this sub-agent fed?" from ``spec`` alone (always available,
    even on the timeout/cancel/exception paths). Detects whether proactive-memory/
    KG content reached the sub-agent context — by current design memory is injected
    only into the *primary* system prompt, so this is typically ``False``, which is
    itself the answer the ticket asks for.

    Args:
        system_content: The fully-built sub-agent system prompt (base + skill index).
        spec: The sub-agent specification.

    Returns:
        A mapping with system/skill/context sizes, a per-message breakdown
        (``role``/``chars``/``content_preview``), and ``memory_in_context``.
    """
    context_messages: list[dict[str, Any]] = []
    context_chars = 0
    memory_in_context = False
    for msg in spec.context:
        content = str(msg.get("content") or "")
        context_chars += len(content)
        if _MEMORY_CONTEXT_MARKER in content:
            memory_in_context = True
        context_messages.append(
            {
                "role": str(msg.get("role") or ""),
                "chars": len(content),
                "content_preview": content[:_CONTEXT_PREVIEW_CHARS],
            }
        )
    return {
        "system_prompt_chars": len(system_content),
        "skill_index_block_chars": len(spec.skill_index_block),
        "context_message_count": len(spec.context),
        "context_chars": context_chars,
        "context_messages": context_messages,
        "memory_in_context": memory_in_context,
    }


def _emit_sub_agent_capture(
    result: SubAgentResult,
    spec: SubAgentSpec,
    context_breakdown: dict[str, Any],
    trace_id: str,
    session_id: str | None,
    eval_mode: bool = False,
) -> None:
    """Build and write the per-sub-agent audit record (FRE-505), best-effort.

    The record is written unconditionally — including eval runs (FRE-523), which
    accidentally aligned with the new uniform contract — and carries ``eval_mode``
    so eval-derived sub-agent activity stays identifiable.

    Args:
        result: The terminal sub-agent result (success, timeout, error, or cancel).
        spec: The sub-agent specification.
        context_breakdown: Output of :func:`_summarize_input_context`.
        trace_id: Parent request trace identifier.
        session_id: Originating session id.
        eval_mode: True when the parent turn originated from an eval run (FRE-523).
    """
    full_output_chars = len(result.full_output)
    digest_chars = len(result.summary)
    truncation_ratio = digest_chars / full_output_chars if full_output_chars else 0.0
    capture = SubAgentCapture(
        trace_id=trace_id,
        session_id=session_id,
        task_id=str(result.task_id),
        timestamp=datetime.now(timezone.utc),
        spec_task=spec.task,
        mode=spec.mode.value,
        model_role=spec.model_role.value,
        max_tokens=spec.max_tokens,
        tools_granted=list(spec.tools),
        tools_used=result.tools_used,
        full_output=result.full_output,
        full_output_chars=full_output_chars,
        injected_digest=result.summary,
        digest_chars=digest_chars,
        truncation_ratio=truncation_ratio,
        success=result.success,
        error=result.error,
        duration_ms=result.duration_ms,
        cost_usd=result.cost_usd,
        eval_mode=eval_mode,
        **context_breakdown,
    )
    write_sub_agent_capture(capture)


async def run_sub_agent(
    spec: SubAgentSpec,
    llm_client: Any,
    trace_id: str,
    concurrency_controller: Any | None = None,
    session_id: str | None = None,
    eval_mode: bool = False,
) -> SubAgentResult:
    """Execute a single sub-agent inference call.

    Args:
        spec: Sub-agent specification from the primary agent.
        llm_client: LLM client instance (LocalLLMClient or LiteLLMClient).
        trace_id: Parent request trace identifier.
        concurrency_controller: Optional concurrency controller for slot management.
        session_id: Originating session id for cost attribution (ADR-0074).
        eval_mode: True when the parent turn originated from an eval run; stamped
            onto the per-sub-agent audit record for EVAL provenance (FRE-523).

    Returns:
        SubAgentResult with summary, metrics, and success status.
    """
    # FRE-517: real UUID so it can key the (trace_id, task_id) route-trace segment row.
    # Stringified once for every wire/log/ES boundary; only SubAgentResult keeps the UUID.
    task_id = uuid.uuid4()
    task_id_str = str(task_id)
    start_ms = int(time.monotonic() * 1000)

    # Build system prompt: base + optional skill index inherited from parent (Phase B).
    # Built before the try so the FRE-505 input-context breakdown is available on every
    # terminal path (success/timeout/exception/cancel), and so cancellation — which
    # raises BaseException, not Exception — can still emit an audit record.
    _system_content = _SUB_AGENT_SYSTEM_PROMPT
    if spec.skill_index_block:
        _system_content = f"{_system_content}\n\n{spec.skill_index_block}"
    _context_breakdown = _summarize_input_context(_system_content, spec)

    logger.info(
        "sub_agent_start",
        task_id=task_id_str,
        task=spec.task,
        output_format=spec.output_format,
        max_tokens=spec.max_tokens,
        timeout=spec.timeout_seconds,
        trace_id=trace_id,
        session_id=session_id,
        **_context_breakdown,
    )

    tools_used: list[str] = []
    call_cost_usd = 0.0
    try:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _system_content},
        ]
        messages.extend(spec.context)
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Task: {spec.task}\n"
                    f"Output format: {spec.output_format}\n"
                    "Respond with the result only."
                ),
            }
        )

        # Single inference call. Capture the raw response so its cost_usd is
        # rolled into the turn meter (FRE-501); _parse_llm_response yields the
        # content for both Mapping and bare-string responses.
        from personal_agent.telemetry.trace import TraceContext

        raw_response = await asyncio.wait_for(
            llm_client.respond(
                role=spec.model_role,
                messages=messages,
                max_tokens=spec.max_tokens,
                trace_ctx=TraceContext(trace_id=trace_id, session_id=session_id),
            ),
            timeout=spec.timeout_seconds,
        )
        response_content = _parse_llm_response(raw_response)
        call_cost_usd = _extract_call_cost(raw_response)
        summary_cap = 2000

        duration_ms = int(time.monotonic() * 1000) - start_ms

        result = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary=response_content[:summary_cap],
            full_output=response_content,
            tools_used=tools_used,
            token_count=len(response_content.split()),
            duration_ms=duration_ms,
            success=True,
            cost_usd=call_cost_usd,
        )

    except asyncio.TimeoutError:
        duration_ms = int(time.monotonic() * 1000) - start_ms
        result = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary="",
            full_output="",
            tools_used=[],
            token_count=0,
            duration_ms=duration_ms,
            success=False,
            error=f"Timeout after {spec.timeout_seconds}s",
        )

    except asyncio.CancelledError:
        # The outer dispatch can cancel us on a global timeout (expansion_controller).
        # CancelledError is a BaseException — not caught by `except Exception` — so we
        # emit the audit record here (FRE-505) and re-raise to preserve cancellation.
        duration_ms = int(time.monotonic() * 1000) - start_ms
        cancelled = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary="",
            full_output="",
            tools_used=tools_used,
            token_count=0,
            duration_ms=duration_ms,
            success=False,
            error="cancelled (global dispatch timeout)",
            cost_usd=call_cost_usd,
        )
        _emit_sub_agent_capture(
            cancelled, spec, _context_breakdown, trace_id, session_id, eval_mode
        )
        raise

    except Exception as exc:
        duration_ms = int(time.monotonic() * 1000) - start_ms
        result = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary="",
            full_output="",
            tools_used=tools_used,
            token_count=0,
            duration_ms=duration_ms,
            success=False,
            error=str(exc),
            cost_usd=call_cost_usd,
        )

    _full_output_chars = len(result.full_output)
    _digest_chars = len(result.summary)
    logger.info(
        "sub_agent_complete",
        task_id=task_id_str,
        success=result.success,
        duration_ms=result.duration_ms,
        token_count=result.token_count,
        digest_chars=_digest_chars,
        full_output_chars=_full_output_chars,
        truncation_ratio=(_digest_chars / _full_output_chars if _full_output_chars else 0.0),
        error=result.error,
        cost_usd=round(result.cost_usd, 6),
        trace_id=trace_id,
        session_id=session_id,
    )

    # FRE-505: durable per-sub-agent audit record (input context + full output +
    # injected digest + truncation ratio) so a decomposition turn is reconstructable
    # from telemetry alone. Best-effort; never raises.
    _emit_sub_agent_capture(result, spec, _context_breakdown, trace_id, session_id, eval_mode)

    return result


def _parse_llm_response(response: Any) -> str:
    """Extract the content string from a respond() result.

    Real ``llm_client.respond`` returns an ``LLMResponse`` mapping; the
    PARALLEL_INFERENCE path and some tests return a bare string. Both are
    handled so callers never assume a shape they didn't get.

    Args:
        response: The value returned by ``llm_client.respond``.

    Returns:
        The response content string.
    """
    if isinstance(response, Mapping):
        return str(response.get("content") or "")
    return str(response)
