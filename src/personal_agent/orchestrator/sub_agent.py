"""Sub-agent runner — executes focused inference calls.

Each sub-agent is a focused task with a constrained context slice: one LLM
call when its spec grants no tools, or a bounded tool loop (FRE-1389) when it
does — never an open-ended agent. The runner acquires a concurrency slot, runs
the inference (and any granted tool calls), and returns a SubAgentResult with
a compressed summary.

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from personal_agent.captains_log.capture import SubAgentCapture, write_sub_agent_capture
from personal_agent.config import settings
from personal_agent.llm_client.types import GenerationProgress, LLMTimeout
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec
from personal_agent.orchestrator.tool_dispatch import (
    dispatch_tool_call,
    get_shared_tool_execution_layer,
)

logger = structlog.get_logger(__name__)

# FRE-1389 AC-5: a sub-agent may state, in its final line, that it lacked a tool
# it needed — it never acquires the tool itself, only reports the gap. The
# expansion controller (not the sub-agent) decides whether to dispatch a
# replacement with an expanded grant. Parsed strictly (exact line prefix) so
# this stays a deterministic signal, not free-text parsing.
_TOOL_GAP_PREFIX = "TOOL_GAP:"
# Bound on how many distinct out-of-grant attempts one call records — a model
# is untrusted input here; this is a defensive cap, not an expected count.
_MAX_REFUSED_TOOL_ATTEMPTS = 10


# Marker the primary injects when proactive-memory/KG entities are in context
# (executor._render_memory_section_with_ids). Scanned in sub-agent context to answer the
# question FRE-505 exists for: "was memory/KG in the sub-agent's input?"
_MEMORY_CONTEXT_MARKER = "## Your Memory Graph"
# Per-message content preview length (mirrors executor.llm_call_messages_debug).
_CONTEXT_PREVIEW_CHARS = 200
# Cap on the digest injected into the parent's synthesis context (FRE-1379:
# shared by the success path and the killed-result path below). FRE-1387: raised
# from 2000 to a circuit breaker sized ~2x the highest full_output_chars ever
# observed (12,987) — high enough that neither the catalog-declared generation
# ceiling (~2048 tokens / ~8,000 chars) nor the now-deleted (FRE-1381)
# settings.sub_agent_max_tokens (4096 / ~16,000 chars) could ever exceed it,
# so a real sub-agent response now fits whole. It still exists as a backstop
# against a shape that has never occurred here — a tool-using sub-agent
# dumping a long tool-call transcript into its response — not to shape
# ordinary output.
_SUMMARY_CAP_CHARS = 25_000

# System prompt for sub-agents: focused, no personality
_SUB_AGENT_SYSTEM_PROMPT = (
    "You are a focused sub-agent executing a specific sub-task. "
    "Be concise and direct. Respond with the requested output format only. "
    "Do not ask follow-up questions. Do not add preamble or explanation "
    "beyond what was requested. "
    "You cannot request additional tools mid-task. If you cannot complete this "
    "task because you lack a specific tool, do the best you can with what you "
    "have, then end your response with a final line reading exactly "
    f'"{_TOOL_GAP_PREFIX} <tool_name>" (one tool name, no other text on that line).'
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


def _extract_tool_calls(response: Any) -> list[dict[str, Any]]:
    """Pull the raw ``tool_calls`` list from an LLM response.

    Args:
        response: The value returned by ``llm_client.respond``.

    Returns:
        The response's ``ToolCall`` list (each ``{id, name, arguments}``), or
        an empty list when absent or the response is a bare string.
    """
    if isinstance(response, Mapping):
        return list(response.get("tool_calls") or [])
    return []


def _build_tool_defs(tool_names: list[str]) -> list[dict[str, Any]] | None:
    """Build OpenAI-format tool definitions restricted to a granted subset.

    Args:
        tool_names: Tool names this sub-agent is granted (``SubAgentSpec.tools``).

    Returns:
        Tool definitions for exactly ``tool_names``, or ``None`` when the list
        is empty — ``None`` (not ``[]``) so ``respond()`` never receives a
        ``tools`` argument for a grant-less sub-agent, preserving today's exact
        no-tools behavior.
    """
    if not tool_names:
        return None
    granted = set(tool_names)
    all_defs = get_shared_tool_execution_layer().registry.get_tool_definitions_for_llm(mode=None)
    defs = [d for d in all_defs if d.get("function", {}).get("name") in granted]
    return defs or None


def _normalize_tool_calls(
    raw_tool_calls: list[dict[str, Any]], round_num: int
) -> list[dict[str, Any]]:
    """Convert response-shaped tool calls into OpenAI assistant-message shape.

    Prefixes each id with the round number so ids stay unique across rounds —
    mirrors ``executor._build_assistant_tool_calls``: server-side parsers
    (e.g. ``tool_call_parser="qwen3"``) commonly regenerate ids from ``call_0``
    every round, and colliding ids across rounds would make history look
    corrupted.

    Args:
        raw_tool_calls: ``ToolCall``-shaped dicts (``id``, ``name``, ``arguments``).
        round_num: This loop round's 1-based iteration number.

    Returns:
        OpenAI-format tool_call dicts (``id``, ``type``, ``function``, ``index``).
    """
    return [
        {
            "id": f"call_r{round_num}_{idx}_{tc['id']}"
            if tc.get("id")
            else f"call_r{round_num}_{idx}",
            "type": "function",
            "function": {"name": tc.get("name", ""), "arguments": tc.get("arguments", "{}")},
            "index": idx,
        }
        for idx, tc in enumerate(raw_tool_calls)
    ]


def _effective_hard_deadline(spec: SubAgentSpec) -> float:
    """Compute the tool loop's overall ``wait_for`` deadline (FRE-1389).

    ``spec.hard_deadline_seconds``/``spec.timeout_seconds`` alone are sized for
    ONE inference call — ``worker_hard_deadline_seconds``'s own description
    states it as "60s generation + 25s queue-wait absorption", from before
    this loop existed. A genuinely multi-round tool-using sub-agent would
    otherwise be killed by that single-call budget well before ever reaching
    its own ``sub_agent_max_tool_iterations`` cap (AC-2's "explicit, distinct
    terminal state" would then rarely fire in practice). When the spec grants
    no tools, no multi-round loop can occur, so the single-call sizing is
    left exactly as it was pre-loop.

    Args:
        spec: The sub-agent specification.

    Returns:
        The deadline in seconds to pass to the outer ``asyncio.wait_for``.
    """
    single_call_deadline = max(
        spec.hard_deadline_seconds or spec.timeout_seconds, spec.timeout_seconds
    )
    if not spec.tools:
        return single_call_deadline
    return max(single_call_deadline, spec.timeout_seconds * settings.sub_agent_max_tool_iterations)


def _extract_stated_tool_gap(content: str) -> tuple[str, str | None]:
    """Strip a trailing ``TOOL_GAP: <name>`` sentinel line from a response.

    Args:
        content: The sub-agent's final text response.

    Returns:
        A tuple of (content with the sentinel line removed, the stated tool
        name or ``None`` if no sentinel line was present). Only the LAST line
        is checked — a strict, deterministic parse (FRE-1389 AC-5), not a
        free-text scan.
    """
    lines = content.rstrip().splitlines()
    if not lines:
        return content, None
    last = lines[-1].strip()
    if not last.startswith(_TOOL_GAP_PREFIX):
        return content, None
    name = last[len(_TOOL_GAP_PREFIX) :].strip()
    if not name:
        return content, None
    remainder = "\n".join(lines[:-1]).rstrip()
    return remainder, name


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
        tools_denied=list(result.denied_tools),
        tools_used=result.tools_used,
        tool_iterations=result.tool_iterations,
        tool_result_chars_absorbed=result.tool_result_chars_absorbed,
        refused_tool_attempts=list(result.refused_tool_attempts),
        stated_tool_gap=result.stated_tool_gap,
        full_output=result.full_output,
        full_output_chars=full_output_chars,
        injected_digest=result.summary,
        digest_chars=digest_chars,
        truncation_ratio=truncation_ratio,
        success=result.success,
        error=result.error,
        duration_ms=result.duration_ms,
        cost_usd=result.cost_usd,
        tokens_generated=result.tokens_generated,
        elapsed_generation_ms=result.elapsed_generation_ms,
        eval_mode=eval_mode,
        **context_breakdown,
    )
    write_sub_agent_capture(capture)


def _warn_if_clipped(result: SubAgentResult, trace_id: str, session_id: str | None) -> None:
    """Log a WARNING when the digest cap actually clipped this result (FRE-1387).

    ``full_output_chars``/``digest_chars``/``truncation_ratio`` were already
    computed and logged at INFO on every terminal path (FRE-505) — and nothing
    consumed them, which is how 60% of sub-agent output went silently
    discarded for three months. This gives a clip its own WARNING-level event
    name, so it is picked up by :data:`personal_agent.telemetry.error_monitor.
    WARNING_EVENT_ALLOWLIST` and surfaced through the ADR-0056 error-pattern
    scan instead of requiring someone to read raw INFO logs to notice it.

    Args:
        result: The terminal sub-agent result to check.
        trace_id: Parent request trace identifier.
        session_id: Originating session id.
    """
    full_output_chars = len(result.full_output)
    digest_chars = len(result.summary)
    if digest_chars >= full_output_chars:
        return
    logger.warning(
        "sub_agent_output_clipped",
        task_id=str(result.task_id),
        trace_id=trace_id,
        session_id=session_id,
        full_output_chars=full_output_chars,
        digest_chars=digest_chars,
        discarded_chars=full_output_chars - digest_chars,
        truncation_ratio=digest_chars / full_output_chars if full_output_chars else 0.0,
        cap_chars=_SUMMARY_CAP_CHARS,
    )


def _killed_result(
    task_id: uuid.UUID,
    spec: SubAgentSpec,
    duration_ms: float,
    progress: GenerationProgress,
    error: str,
    cost_usd: float = 0.0,
    tools_used: list[str] | None = None,
    tool_iterations: int = 0,
    tool_result_chars_absorbed: int = 0,
    refused_tool_attempts: tuple[str, ...] = (),
) -> SubAgentResult:
    """Build a SubAgentResult for a sub-agent that never returned (FRE-1379).

    Recovers whatever the streaming client captured into ``progress`` before
    the call was cancelled out from under it, so a killed worker still reports
    partial content, an estimated token count, and generation-only elapsed
    time — instead of the empty ``digest_chars=0, full_output_chars=0`` record
    every timeout/cancellation produced before this. Shared by the outer
    hard-deadline timeout, the inner generation-budget timeout, and a global
    dispatch cancellation — all three are the same "killed with whatever
    progress was captured" shape, just different triggers.

    Args:
        task_id: This sub-agent invocation's identifier.
        spec: The sub-agent specification.
        duration_ms: Wall-clock time since spawn.
        progress: Whatever the streaming client recorded before cancellation.
        error: Human-readable reason, distinguishing which budget fired.
        cost_usd: Cost of every COMPLETED round before the kill, summed by the
            caller (FRE-1389 AC-6) — 0.0 unless the caller tracked one.
        tools_used: Tools actually dispatched in completed rounds before the
            kill (FRE-1389). ``None`` normalizes to an empty list.
        tool_iterations: Tool-execution rounds completed before the kill.
        tool_result_chars_absorbed: Raw tool-result chars absorbed in
            completed rounds before the kill.
        refused_tool_attempts: Out-of-grant attempts refused in completed
            rounds before the kill.

    Returns:
        A failed SubAgentResult carrying whatever partial state is available.
    """
    partial = progress.content
    elapsed_generation_ms = (
        (time.monotonic() - progress.generation_started_monotonic) * 1000
        if progress.generation_started_monotonic is not None
        else None
    )
    return SubAgentResult(
        task_id=task_id,
        spec_task=spec.task,
        summary=partial[:_SUMMARY_CAP_CHARS],
        full_output=partial,
        tools_used=tools_used if tools_used is not None else [],
        token_count=0,
        tokens_generated=len(partial.split()) if partial else 0,
        elapsed_generation_ms=elapsed_generation_ms,
        duration_ms=duration_ms,
        success=False,
        error=error,
        cost_usd=cost_usd,
        denied_tools=spec.denied_tools,
        tool_iterations=tool_iterations,
        tool_result_chars_absorbed=tool_result_chars_absorbed,
        refused_tool_attempts=refused_tool_attempts,
    )


@dataclass
class _ToolLoopState:
    """Mutable tool-loop accumulator, external to the loop coroutine's own frame.

    A cancellation mid-loop (the outer hard-deadline ``wait_for``, FRE-1379)
    destroys the cancelled coroutine's local frame — exactly why
    ``GenerationProgress`` exists for the single-call case. This extends the
    same pattern across a multi-round loop (FRE-1389): every completed
    round's activity lands here as it happens, so a kill mid-round still
    reports every prior round's cost/tools/chars, not just the in-flight one.
    """

    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    tool_iterations: int = 0
    tool_result_chars_absorbed: int = 0
    refused_tool_attempts: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    progress: GenerationProgress = field(default_factory=GenerationProgress)


class _ToolIterationLimitReached(Exception):
    """Raised when the sub-agent's own tool-loop cap (AC-2) is hit.

    Carries whatever text accompanied the refused batch so the caller can
    still report it — the sub-agent stays a pure bounded function: no
    injected "please wrap up" round, just a stop.
    """

    def __init__(self, partial_content: str) -> None:
        super().__init__("sub-agent tool iteration limit reached")
        self.partial_content = partial_content


async def _run_tool_loop(
    state: _ToolLoopState,
    spec: SubAgentSpec,
    llm_client: Any,
    tool_defs: list[dict[str, Any]] | None,
    tool_layer: Any,
    loaded_skills: set[str],
    trace_id: str,
    session_id: str | None,
) -> tuple[str, str | None]:
    """Run inference/tool-execution rounds until the model stops or the cap fires.

    Mutates ``state`` in place after every completed round so a caller that
    cancels this coroutine mid-round still sees every prior round's activity.
    A tool call for a name outside ``spec.tools`` is refused without dispatch
    (AC-3); refused names are deduplicated and bounded via
    ``_MAX_REFUSED_TOOL_ATTEMPTS`` since they are untrusted model output.

    Args:
        state: Mutable accumulator, updated as each round completes.
        spec: The sub-agent specification (task, tools, model_role).
        llm_client: LLM client instance.
        tool_defs: OpenAI-format tool definitions restricted to ``spec.tools``,
            or ``None`` for a grant-less sub-agent.
        tool_layer: Shared ``ToolExecutionLayer`` for real dispatch.
        loaded_skills: Mutable ``read_skill`` dedup set for ``dispatch_tool_call``.
        trace_id: Parent request trace identifier.
        session_id: Originating session id.

    Returns:
        (final response text with any ``TOOL_GAP`` sentinel stripped, the
        stated gap tool name or ``None``).

    Raises:
        _ToolIterationLimitReached: When another tool batch would exceed
            ``settings.sub_agent_max_tool_iterations``.
    """
    from personal_agent.telemetry.trace import TraceContext

    tool_choice = "auto" if tool_defs else None
    while True:
        round_progress = GenerationProgress()
        state.progress = round_progress
        raw_response = await llm_client.respond(
            role=spec.model_role,
            messages=state.messages,
            max_tokens=spec.max_tokens,
            trace_ctx=TraceContext(trace_id=trace_id, session_id=session_id),
            timeout_s=spec.timeout_seconds,
            progress_sink=round_progress,
            tools=tool_defs,
            tool_choice=tool_choice,
        )
        state.cost_usd += _extract_call_cost(raw_response)
        raw_tool_calls = _extract_tool_calls(raw_response)
        response_content = _parse_llm_response(raw_response)

        if not raw_tool_calls:
            return _extract_stated_tool_gap(response_content)

        if state.tool_iterations >= settings.sub_agent_max_tool_iterations:
            raise _ToolIterationLimitReached(response_content)

        state.tool_iterations += 1
        normalized_calls = _normalize_tool_calls(raw_tool_calls, state.tool_iterations)
        state.messages.append(
            {"role": "assistant", "content": response_content, "tool_calls": normalized_calls}
        )

        for call, raw_call in zip(normalized_calls, raw_tool_calls, strict=True):
            tool_call_id = call["id"]
            tool_name = call["function"]["name"]

            if tool_name not in spec.tools:
                if len(state.refused_tool_attempts) < _MAX_REFUSED_TOOL_ATTEMPTS:
                    state.refused_tool_attempts.append(tool_name)
                error_content = json.dumps(
                    {"status": "error", "hint": f"{tool_name} is not available to this sub-agent."}
                )
                state.tool_result_chars_absorbed += len(error_content)
                state.messages.append(
                    {
                        "tool_call_id": tool_call_id,
                        "role": "tool",
                        "name": tool_name,
                        "content": error_content,
                    }
                )
                continue

            try:
                arguments = json.loads(raw_call.get("arguments") or "{}")
            except json.JSONDecodeError:
                error_content = json.dumps(
                    {
                        "status": "retry",
                        "hint": "Arguments were not valid JSON. Retry with valid JSON arguments.",
                    }
                )
                state.tool_result_chars_absorbed += len(error_content)
                state.messages.append(
                    {
                        "tool_call_id": tool_call_id,
                        "role": "tool",
                        "name": tool_name,
                        "content": error_content,
                    }
                )
                continue

            dispatch_result = await dispatch_tool_call(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                tool_layer=tool_layer,
                trace_ctx=TraceContext(trace_id=trace_id, session_id=session_id),
                trace_id=trace_id,
                session_id=session_id,
                loaded_skills=loaded_skills,
            )
            state.tools_used.append(tool_name)
            content = str(dispatch_result["content"])
            state.tool_result_chars_absorbed += len(content)
            state.messages.append(
                {
                    "tool_call_id": tool_call_id,
                    "role": "tool",
                    "name": tool_name,
                    "content": content,
                }
            )


async def run_sub_agent(
    spec: SubAgentSpec,
    llm_client: Any,
    trace_id: str,
    concurrency_controller: Any | None = None,
    session_id: str | None = None,
    eval_mode: bool = False,
    max_deadline_seconds: float | None = None,
) -> SubAgentResult:
    """Execute a single sub-agent inference call.

    Args:
        spec: Sub-agent specification from the primary agent.
        llm_client: LLM client instance (LiteLLMClient).
        trace_id: Parent request trace identifier.
        concurrency_controller: Optional concurrency controller for slot management.
        session_id: Originating session id for cost attribution (ADR-0074).
        eval_mode: True when the parent turn originated from an eval run; stamped
            onto the per-sub-agent audit record for EVAL provenance (FRE-523).
        max_deadline_seconds: Caller-supplied ceiling on the outer ``wait_for``
            deadline (FRE-1397) — a dispatcher with a shrinking turn budget
            passes what is actually left so a serialized fan-out cannot
            outlive the turn. Only ever shrinks ``_effective_hard_deadline``'s
            own result via ``min()``, never extends it. ``None`` (the
            default) leaves that deadline exactly as computed today.

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

    # FRE-1389: tool defs restricted to exactly this spec's granted subset —
    # None (not []) when spec.tools is empty, so respond() never receives a
    # tools argument for a grant-less sub-agent, preserving pre-loop behavior.
    tool_layer = get_shared_tool_execution_layer()
    tool_defs = _build_tool_defs(spec.tools)
    loaded_skills: set[str] = set(spec.loaded_skills)

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
    # External to the loop coroutine's own frame (see _ToolLoopState) so a
    # cancellation mid-round still leaves every completed round's activity
    # readable from here in the except blocks below.
    state = _ToolLoopState(messages=messages)

    try:
        # FRE-1374: timeout_s reaches the client as the GENERATION-only budget — for
        # LiteLLMClient it becomes the read timeout applied inside its concurrency-slot
        # context, so it starts counting at slot acquisition, not at spawn. The outer
        # wait_for below bounds the ENTIRE tool loop (every round's inference and tool
        # execution, FRE-1389) — a separate, larger, explicitly-named safety net (not
        # the primary timeout mechanism) for a client that ignores timeout_s. Scaled by
        # the iteration cap for a tool-granted spec (_effective_hard_deadline) — the
        # single-call sizing alone would kill a genuine multi-round loop before it ever
        # reached its own cap.
        hard_deadline = _effective_hard_deadline(spec)
        if max_deadline_seconds is not None:
            hard_deadline = min(hard_deadline, max_deadline_seconds)
        response_content, stated_tool_gap = await asyncio.wait_for(
            _run_tool_loop(
                state, spec, llm_client, tool_defs, tool_layer, loaded_skills, trace_id, session_id
            ),
            timeout=hard_deadline,
        )

        duration_ms = int(time.monotonic() * 1000) - start_ms
        elapsed_generation_ms = (
            (time.monotonic() - state.progress.generation_started_monotonic) * 1000
            if state.progress.generation_started_monotonic is not None
            else None
        )

        result = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary=response_content[:_SUMMARY_CAP_CHARS],
            full_output=response_content,
            tools_used=state.tools_used,
            token_count=len(response_content.split()),
            tokens_generated=len(response_content.split()),
            elapsed_generation_ms=elapsed_generation_ms,
            duration_ms=duration_ms,
            success=True,
            cost_usd=state.cost_usd,
            denied_tools=spec.denied_tools,
            tool_iterations=state.tool_iterations,
            tool_result_chars_absorbed=state.tool_result_chars_absorbed,
            refused_tool_attempts=tuple(dict.fromkeys(state.refused_tool_attempts)),
            stated_tool_gap=stated_tool_gap,
        )

    except _ToolIterationLimitReached as exc:
        # AC-2: an explicit, distinct terminal state — not a disguised success —
        # so a caller's failure/degradation checks see an incomplete worker as
        # incomplete, not as having answered.
        duration_ms = int(time.monotonic() * 1000) - start_ms
        result = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary=exc.partial_content[:_SUMMARY_CAP_CHARS],
            full_output=exc.partial_content,
            tools_used=state.tools_used,
            token_count=len(exc.partial_content.split()),
            tokens_generated=len(exc.partial_content.split()),
            duration_ms=duration_ms,
            success=False,
            error=f"tool iteration limit reached after {state.tool_iterations} rounds",
            cost_usd=state.cost_usd,
            denied_tools=spec.denied_tools,
            tool_iterations=state.tool_iterations,
            tool_result_chars_absorbed=state.tool_result_chars_absorbed,
            refused_tool_attempts=tuple(dict.fromkeys(state.refused_tool_attempts)),
        )

    except asyncio.TimeoutError:
        duration_ms = int(time.monotonic() * 1000) - start_ms
        result = _killed_result(
            task_id,
            spec,
            duration_ms,
            state.progress,
            # FRE-1374 (AC-2): report the measured elapsed time, not the nominal
            # budget — the hard deadline that actually fired may differ from
            # spec.timeout_seconds, and the old hard-coded value hid exactly the
            # shortfall this ticket exists to make visible.
            error=f"Timeout after {duration_ms / 1000:.1f}s",
            cost_usd=state.cost_usd,
            tools_used=state.tools_used,
            tool_iterations=state.tool_iterations,
            tool_result_chars_absorbed=state.tool_result_chars_absorbed,
            refused_tool_attempts=tuple(dict.fromkeys(state.refused_tool_attempts)),
        )

    except LLMTimeout as exc:
        # FRE-1379: the client's own wall-clock generation budget fired before
        # the outer hard-deadline above did — this is the common case now that
        # the local streaming path enforces spec.timeout_seconds as a real
        # duration bound, not just a read timeout. Distinct wording from the
        # outer branch so a reader can tell which budget fired without
        # cross-referencing durations.
        duration_ms = int(time.monotonic() * 1000) - start_ms
        result = _killed_result(
            task_id,
            spec,
            duration_ms,
            state.progress,
            error=f"Timeout after {duration_ms / 1000:.1f}s (generation budget): {exc}",
            cost_usd=state.cost_usd,
            tools_used=state.tools_used,
            tool_iterations=state.tool_iterations,
            tool_result_chars_absorbed=state.tool_result_chars_absorbed,
            refused_tool_attempts=tuple(dict.fromkeys(state.refused_tool_attempts)),
        )

    except asyncio.CancelledError:
        # The outer dispatch can cancel us on a global timeout (expansion_controller).
        # CancelledError is a BaseException — not caught by `except Exception` — so we
        # emit the audit record here (FRE-505) and re-raise to preserve cancellation.
        duration_ms = int(time.monotonic() * 1000) - start_ms
        cancelled = _killed_result(
            task_id,
            spec,
            duration_ms,
            state.progress,
            error="cancelled (global dispatch timeout)",
            cost_usd=state.cost_usd,
            tools_used=state.tools_used,
            tool_iterations=state.tool_iterations,
            tool_result_chars_absorbed=state.tool_result_chars_absorbed,
            refused_tool_attempts=tuple(dict.fromkeys(state.refused_tool_attempts)),
        )
        _emit_sub_agent_capture(
            cancelled, spec, _context_breakdown, trace_id, session_id, eval_mode
        )
        _warn_if_clipped(cancelled, trace_id, session_id)
        raise

    except Exception as exc:
        duration_ms = int(time.monotonic() * 1000) - start_ms
        result = _killed_result(
            task_id,
            spec,
            duration_ms,
            state.progress,
            error=str(exc),
            cost_usd=state.cost_usd,
            tools_used=state.tools_used,
            tool_iterations=state.tool_iterations,
            tool_result_chars_absorbed=state.tool_result_chars_absorbed,
            refused_tool_attempts=tuple(dict.fromkeys(state.refused_tool_attempts)),
        )

    _full_output_chars = len(result.full_output)
    _digest_chars = len(result.summary)
    logger.info(
        "sub_agent_complete",
        task_id=task_id_str,
        success=result.success,
        token_count=result.token_count,
        digest_chars=_digest_chars,
        full_output_chars=_full_output_chars,
        truncation_ratio=(_digest_chars / _full_output_chars if _full_output_chars else 0.0),
        error=result.error,
        cost_usd=round(result.cost_usd, 6),
        tokens_generated=result.tokens_generated,
        elapsed_generation_ms=result.elapsed_generation_ms,
        trace_id=trace_id,
        session_id=session_id,
    )

    # FRE-505: durable per-sub-agent audit record (input context + full output +
    # injected digest + truncation ratio) so a decomposition turn is reconstructable
    # from telemetry alone. Best-effort; never raises.
    _emit_sub_agent_capture(result, spec, _context_breakdown, trace_id, session_id, eval_mode)
    _warn_if_clipped(result, trace_id, session_id)

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
