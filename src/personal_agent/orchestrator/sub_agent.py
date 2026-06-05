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
import json
import time
import uuid
from collections.abc import Mapping
from typing import Any

import structlog

from personal_agent.config import settings
from personal_agent.orchestrator.expansion_types import SubAgentMode
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec
from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call

logger = structlog.get_logger(__name__)

# System prompt for sub-agents: focused, no personality
_SUB_AGENT_SYSTEM_PROMPT = (
    "You are a focused sub-agent executing a specific sub-task. "
    "Be concise and direct. Respond with the requested output format only. "
    "Do not ask follow-up questions. Do not add preamble or explanation "
    "beyond what was requested."
)

# ADR-0086 D3 — read-only discovery tool surface for TOOLED_SEQUENTIAL sub-agents.
# A *name* allowlist, not a category filter: a category denylist is unsound because
# `bash` and `run_python` share the `system_dangerous` category, so a category rule
# cannot both admit `bash` (the ADR's primary discovery tool) and reject `run_python`.
# Any tool not in this set is rejected before dispatch — never a mutating tool
# (`write`/`edit`/`artifact_write`) and never the expansion path itself (no recursion).
# NOTE (owner steer, 2026-06-05): this static allowlist is a placeholder for a future
# HITL dynamic allow-gate where a human approval boundary authorizes dangerous-category
# calls per-invocation. Until that lands, `bash` is admitted statically and runs through
# the same `execute_tool` action-boundary governance as the primary executor.
_DISCOVERY_TOOL_ALLOWLIST = frozenset(
    {"bash", "read", "read_skill", "web_search", "recall_personal_history"}
)


async def run_sub_agent(
    spec: SubAgentSpec,
    llm_client: Any,
    trace_id: str,
    concurrency_controller: Any | None = None,
    session_id: str | None = None,
) -> SubAgentResult:
    """Execute a single sub-agent inference call.

    Args:
        spec: Sub-agent specification from the primary agent.
        llm_client: LLM client instance (LocalLLMClient or LiteLLMClient).
        trace_id: Parent request trace identifier.
        concurrency_controller: Optional concurrency controller for slot management.
        session_id: Originating session id for cost attribution (ADR-0074).

    Returns:
        SubAgentResult with summary, metrics, and success status.
    """
    task_id = f"sub-{uuid.uuid4().hex[:12]}"
    start_ms = int(time.monotonic() * 1000)

    logger.info(
        "sub_agent_start",
        task_id=task_id,
        task=spec.task,
        output_format=spec.output_format,
        max_tokens=spec.max_tokens,
        timeout=spec.timeout_seconds,
        trace_id=trace_id,
    )

    try:
        # Build system prompt: base + optional skill index inherited from parent (Phase B)
        _system_content = _SUB_AGENT_SYSTEM_PROMPT
        if spec.skill_index_block:
            _system_content = f"{_system_content}\n\n{spec.skill_index_block}"

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

        tools_used: list[str] = []
        if spec.mode == SubAgentMode.TOOLED_SEQUENTIAL and spec.tools:
            # Tooled mode: real bounded tool-use loop (ADR-0086 D3).
            response_content, tools_used = await _run_tooled_loop(
                messages=messages,
                llm_client=llm_client,
                spec=spec,
                trace_id=trace_id,
                task_id=task_id,
                session_id=session_id,
            )
            # ADR-0086 D4 / owner steer (2026-06-05): no premature digest. Keep the
            # parent-facing summary generous so we observe real discovery output;
            # full_output is preserved uncapped for ES observability.
            summary_cap = settings.sub_agent_summary_max_chars
        else:
            # Default: single inference call
            from personal_agent.telemetry.trace import TraceContext

            response_content = str(
                await asyncio.wait_for(
                    llm_client.respond(
                        role=spec.model_role,
                        messages=messages,
                        max_tokens=spec.max_tokens,
                        trace_ctx=TraceContext(trace_id=trace_id, session_id=session_id),
                    ),
                    timeout=spec.timeout_seconds,
                )
            )
            summary_cap = 2000

        duration_ms = int(time.monotonic() * 1000) - start_ms

        # A tooled discovery sub-agent that finishes with no content (exhausted its
        # iteration ceiling without a usable synthesis, or hit max_tokens) produced
        # no digest. Surface that to the parent as a failure rather than a silent
        # empty success it would synthesize around (master review #1).
        is_tooled = spec.mode == SubAgentMode.TOOLED_SEQUENTIAL and bool(spec.tools)
        empty_digest = is_tooled and not response_content.strip()

        result = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary=response_content[:summary_cap],
            full_output=response_content,
            tools_used=tools_used,
            token_count=len(response_content.split()),
            duration_ms=duration_ms,
            success=not empty_digest,
            error="discovery sub-agent produced an empty digest" if empty_digest else None,
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

    except Exception as exc:
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
            error=str(exc),
        )

    logger.info(
        "sub_agent_complete",
        task_id=task_id,
        success=result.success,
        duration_ms=result.duration_ms,
        token_count=result.token_count,
        error=result.error,
        trace_id=trace_id,
    )

    return result


def _parse_llm_response(response: Any) -> tuple[str, list[Mapping[str, Any]]]:
    """Extract (content, tool_calls) from a respond() result.

    Real ``llm_client.respond`` returns an ``LLMResponse`` mapping; the
    PARALLEL_INFERENCE path and some tests return a bare string. Both are
    handled so the tooled loop never assumes a shape it didn't get.

    Args:
        response: The value returned by ``llm_client.respond``.

    Malformed tool-call entries (non-Mapping) are dropped here so a single bad
    element never crashes the loop and discards the whole slice (master review #3).

    Returns:
        A tuple of (content string, list of tool-call dicts ``{id,name,arguments}``).
    """
    if isinstance(response, Mapping):
        content = str(response.get("content") or "")
        tool_calls = [tc for tc in (response.get("tool_calls") or []) if isinstance(tc, Mapping)]
        return content, tool_calls
    return str(response), []


def _to_openai_tool_calls(tool_calls: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize ``LLMResponse`` tool calls to OpenAI assistant-message wire format.

    ``LLMResponse.tool_calls`` items are flat ``{id, name, arguments}``; the
    transcript assistant message and the downstream tool results expect the
    nested ``{id, type, function:{name, arguments}}`` form.

    Args:
        tool_calls: Flat tool-call dicts from the LLM response.

    Returns:
        OpenAI-format tool-call dicts suitable for an assistant message.
    """
    normalized: list[dict[str, Any]] = []
    for idx, tc in enumerate(tool_calls):
        normalized.append(
            {
                "id": tc.get("id") or f"call_{idx}",
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": tc.get("arguments", "{}"),
                },
                "index": idx,
            }
        )
    return normalized


async def _run_tooled_loop(
    messages: list[dict[str, Any]],
    llm_client: Any,
    spec: SubAgentSpec,
    trace_id: str,
    task_id: str,
    session_id: str | None = None,
) -> tuple[str, list[str]]:
    """Run a real bounded tool-use loop for TOOLED_SEQUENTIAL sub-agents (ADR-0086 D3).

    The sub-agent calls read-only discovery tools, incorporates their results in
    its OWN message list (isolated from the parent context, ADR-0086 D4), and
    re-prompts until it returns a final answer or the iteration ceiling is hit.
    Tool calls are dispatched through ``dispatch_tool_call`` — the SAME path the
    primary executor uses — so governance (ADR-0063) and telemetry (ADR-0074) are
    inherited, not re-implemented. Tools outside ``_DISCOVERY_TOOL_ALLOWLIST`` (or
    not granted in ``spec.tools``) are rejected before dispatch.

    Args:
        messages: Initial message context (copied; the parent list is untouched).
        llm_client: LLM client.
        spec: Sub-agent specification (carries allowed ``tools``).
        trace_id: Trace identifier.
        task_id: Sub-agent task identifier.
        session_id: Originating session id for cost attribution (ADR-0074).

    Returns:
        A tuple of (final response content, list of tool names actually executed).
    """
    from personal_agent.telemetry.trace import TraceContext
    from personal_agent.tools import ToolExecutionLayer, get_default_registry

    trace_ctx = TraceContext(trace_id=trace_id, session_id=session_id)
    registry = get_default_registry()
    tool_layer = ToolExecutionLayer(registry)
    loaded_skills: set[str] = set(spec.loaded_skills)

    # The grant ∩ allowlist is the only surface this sub-agent may execute.
    allowed_names = [t for t in spec.tools if t in _DISCOVERY_TOOL_ALLOWLIST]
    rejected_grants = [t for t in spec.tools if t not in _DISCOVERY_TOOL_ALLOWLIST]
    if rejected_grants:
        logger.warning(
            "sub_agent_tools_filtered",
            task_id=task_id,
            rejected=rejected_grants,
            trace_id=trace_id,
        )
    tool_defs = [
        d
        for d in registry.get_tool_definitions_for_llm()
        if d.get("function", {}).get("name") in allowed_names
    ]

    loop_messages = list(messages)
    tools_used: list[str] = []
    max_iterations = settings.sub_agent_max_tool_iterations

    for iteration in range(max_iterations):
        response = await asyncio.wait_for(
            llm_client.respond(
                role=spec.model_role,
                messages=loop_messages,
                max_tokens=spec.max_tokens,
                tools=tool_defs or None,
                trace_ctx=trace_ctx,
            ),
            timeout=spec.timeout_seconds,
        )

        content, tool_calls = _parse_llm_response(response)

        if not tool_calls:
            # Model produced a final answer — discovery complete.
            return content, tools_used

        # Record the assistant turn (with normalized tool_calls) in the loop's
        # own transcript so the model sees its requests alongside the results.
        openai_tool_calls = _to_openai_tool_calls(tool_calls)
        loop_messages.append(
            {"role": "assistant", "content": content, "tool_calls": openai_tool_calls}
        )

        for raw_tc, otc in zip(tool_calls, openai_tool_calls, strict=True):
            tool_name = str(raw_tc.get("name", ""))
            tool_call_id = otc["id"]

            # Read-only enforcement: reject anything outside the granted allowlist.
            if tool_name not in allowed_names:
                logger.warning(
                    "sub_agent_tool_rejected",
                    task_id=task_id,
                    tool_name=tool_name,
                    trace_id=trace_id,
                )
                loop_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": tool_name,
                        "content": json.dumps(
                            {
                                "status": "rejected",
                                "hint": (
                                    f"{tool_name} is not permitted for read-only "
                                    "discovery sub-agents. Use only: "
                                    f"{', '.join(allowed_names) or 'none'}."
                                ),
                            }
                        ),
                    }
                )
                continue

            try:
                arguments = json.loads(raw_tc.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments = {}

            dispatch_result = await dispatch_tool_call(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                tool_layer=tool_layer,
                trace_ctx=trace_ctx,
                trace_id=trace_id,
                session_id=session_id,
                loaded_skills=loaded_skills,
            )
            tools_used.append(tool_name)
            loop_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": dispatch_result["content"],
                }
            )

        logger.info(
            "sub_agent_tooled_iteration",
            task_id=task_id,
            iteration=iteration,
            tool_count=len(tool_calls),
            trace_id=trace_id,
        )

    # Iteration ceiling reached — force a final synthesis. Tools are disabled by
    # OMITTING the tools= argument entirely: with no tool surface offered the model
    # cannot tool-call and must synthesize a digest from gathered results (mirrors
    # the primary's force_synthesis_from_limit). NOTE: we do NOT pass
    # tool_choice="none" — both adapters gate tool_choice behind `if tools:`, so it
    # would never reach the wire; "no tools offered" is the enforced guarantee
    # (master review #2). ADR-0086 D3.
    logger.info(
        "sub_agent_tooled_ceiling",
        task_id=task_id,
        max_iterations=max_iterations,
        trace_id=trace_id,
    )
    final_response = await asyncio.wait_for(
        llm_client.respond(
            role=spec.model_role,
            messages=loop_messages,
            max_tokens=spec.max_tokens,
            trace_ctx=trace_ctx,
        ),
        timeout=spec.timeout_seconds,
    )
    content, _ = _parse_llm_response(final_response)
    return content, tools_used
