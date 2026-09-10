"""Sub-agent runner — executes focused inference calls.

Each sub-agent is a focused task with a constrained context slice: one LLM
call when its spec grants no tools, or a bounded tool loop (FRE-1389) when it
does — never an open-ended agent. The runner acquires a concurrency slot, runs
the inference (and any granted tool calls), and returns a SubAgentResult with
a compressed summary.

Full output goes to ES via structlog; only the summary enters
the primary agent's synthesis context.

Identity (FRE-1467)
-------------------
One ``TraceContext`` is built per sub-agent, in :func:`run_sub_agent`, and is
used both for the sub-agent's own inference call and for every tool it
dispatches. It carries the turn's ``user_id``, ``authenticated`` and
``eval_mode`` — the same five fields the primary's own context carries
(``executor.run_task``). It was formerly two separate constructions carrying
neither identity field, which put every identity-scoped tool out of a
sub-agent's reach.

A sub-agent's read is therefore now close to the primary's, though no longer equal on every
tool: FRE-1473 gave ``recall_personal_history`` a sub-agent-only parameter ceiling (see
``dispatch_tool_call``'s ``principal="sub_agent"`` argument and
``governance.sub_agent_tools.clamp_sub_agent_tool_params``), so a sub-agent's window and
turn count are capped tighter than the primary's while the read itself uses the same
identity. What identity changed, per granted tool:

- ``search_memory`` — ``MemoryService.query_claims`` and
  ``query_claims_history`` stop returning ``[]`` on their missing-identity
  guard, so the user's own ``:Claim`` rows are returned. ``query_stance_history``
  stops fail-closing on ``authenticated``; note it is scoped to the harness
  owner's ``Person {is_owner: true}`` sentinel, not to the connecting
  ``user_id`` (ADR-0098 D2/D3), so what identity unlocks there is the
  ``authenticated`` gate alone. The FRE-229 visibility filter admits ``group``
  rows and this user's ``private:`` rows to entity-match and broad-recall
  results.
- ``recall_personal_history`` — stops raising ``missing_user_id`` on every call.
  Returns the user's own past turns in the window: ``turn_id``, timestamp,
  session id, ``user_message`` and ``assistant_response`` (each capped at 400
  characters, the same bound ``search_memory`` applies to a matched turn),
  ``summary``, discussed entities, and an optional topic-match flag. The window
  and turn count are the sub-agent-only ceiling above, not the tool's own
  365-day/50-turn range — that range still applies to the primary unchanged.
- ``web_search`` — unchanged. It reads ``ctx.trace_id`` and no identity field.
- ``run_python`` — unchanged. Same: ``ctx.trace_id`` only.

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
from uuid import UUID

import structlog

from personal_agent.captains_log.capture import SubAgentCapture, write_sub_agent_capture
from personal_agent.config import settings
from personal_agent.llm_client.types import GenerationProgress, LLMTimeout
from personal_agent.orchestrator.sub_agent_approval import (
    get_sub_agent_approval_broker,
    resolve_sub_agent_approval_requirements,
)
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec
from personal_agent.orchestrator.tool_dispatch import (
    dispatch_tool_call,
    get_shared_tool_execution_layer,
)
from personal_agent.telemetry.trace import TraceContext

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


def _resolve_effective_timeout(spec: SubAgentSpec, llm_client: Any) -> float:
    """Resolve the ONE generation budget this sub-agent call uses (ADR-0145 D1).

    The budget belongs to the role, and the client already holds the role's effective
    definition, so the client is asked for it rather than a setting being read here.
    The number is resolved once and used twice: the outer deadline is sized from it
    (:func:`_effective_hard_deadline`), and it is handed back down to the client as
    ``timeout_s`` for the inner call. Resolving it separately at each use is the
    two-sources-of-truth condition FRE-1444 exists to remove. D1 sketched the second use
    as omitting ``timeout_s`` and letting the client re-derive the value; only the local
    dispatch branch does that, so it is passed explicitly instead — see
    :func:`_run_tool_loop`'s ``effective_timeout`` argument for the full reasoning.

    Args:
        spec: The sub-agent specification.
        llm_client: The client this sub-agent dispatches through.

    Returns:
        The generation budget in seconds.

    Raises:
        ValueError: When neither the spec nor the client names a budget. Every
            factory-built client carries a definition and therefore names one, so this
            is an unconfigurable sub-agent rather than a routine fallback — inventing a
            number here would restore the defect.
    """
    if spec.timeout_seconds is not None:
        return float(spec.timeout_seconds)
    declared = getattr(llm_client, "default_timeout_seconds", None)
    if declared is None:
        raise ValueError(
            "sub-agent has no generation budget: the spec names none and "
            f"{type(llm_client).__name__} declares no default_timeout_seconds "
            "(ADR-0145 D1)."
        )
    return float(declared)


def _effective_hard_deadline(spec: SubAgentSpec, effective_timeout: float) -> float:
    """Compute the tool loop's overall ``wait_for`` deadline (FRE-1389).

    The single-call sizing is the generation budget plus
    ``settings.worker_queue_absorption_seconds``. Deriving it, rather than reading a
    fixed number, is what keeps the declared queue-wait allowance intact for any
    binding value (ADR-0145 D1): a fixed 85 against a 90s role budget gives
    ``max(85, 90) = 90``, which collapses the net onto the generation timeout and makes
    it inert.

    That single-call budget is sized for ONE inference call. A genuinely multi-round
    tool-using sub-agent would otherwise be killed by it well before ever reaching its
    own ``sub_agent_max_tool_iterations`` cap (AC-2's "explicit, distinct terminal
    state" would then rarely fire in practice). When the spec grants no tools, no
    multi-round loop can occur, so the single-call sizing applies unscaled.

    Args:
        spec: The sub-agent specification.
        effective_timeout: The generation budget resolved by
            :func:`_resolve_effective_timeout`.

    Returns:
        The deadline in seconds to pass to the outer ``asyncio.wait_for``.
    """
    # `is not None`, not `or`: an explicit 0.0 means "no absorption margin, just the
    # generation budget" — which the max() below then floors correctly. Falling through
    # on 0.0 would silently grant the caller 25s MORE than it asked for.
    single_call_deadline = max(
        spec.hard_deadline_seconds
        if spec.hard_deadline_seconds is not None
        else effective_timeout + settings.worker_queue_absorption_seconds,
        effective_timeout,
    )
    if not spec.tools:
        return single_call_deadline
    return max(single_call_deadline, effective_timeout * settings.sub_agent_max_tool_iterations)


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
        narrative_synthesized=result.narrative_synthesized,
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


def _warn_if_narrative_synthesized(
    result: SubAgentResult, trace_id: str, session_id: str | None
) -> None:
    """Log a WARNING when a capped worker's report is a synthesized fallback (FRE-1399).

    Mirrors :func:`_warn_if_clipped`'s shape for the digest-cap case: the flag
    alone distinguishes "this worker found nothing" from "this worker did
    work and could not report it" (AC-4), and this gives that distinction its
    own WARNING-level event so it is visible without comparing
    ``tool_result_chars_absorbed`` against ``summary`` by hand (AC-3).

    Args:
        result: The terminal sub-agent result to check.
        trace_id: Parent request trace identifier.
        session_id: Originating session id.
    """
    if not result.narrative_synthesized:
        return
    logger.warning(
        "sub_agent_iteration_cap_narrative_synthesized",
        task_id=str(result.task_id),
        trace_id=trace_id,
        session_id=session_id,
        tool_iterations=result.tool_iterations,
        tool_result_chars_absorbed=result.tool_result_chars_absorbed,
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
    # FRE-1399: every round's own assistant text (including the round that trips the
    # iteration cap), so a capped worker's report is never limited to just that one
    # round's completion — which is frequently empty when a tool-call round carries
    # no accompanying text. Only non-whitespace text is kept (see _run_tool_loop).
    round_texts: list[str] = field(default_factory=list)


class _ToolIterationLimitReached(Exception):
    """Raised when the sub-agent's own tool-loop cap (AC-2) is hit.

    Carries whatever text the loop recovered so the caller can still report
    it — the sub-agent stays a pure bounded function: no injected "please
    wrap up" round, just a stop. ``narrative_synthesized`` (FRE-1399) tells
    the caller whether ``partial_content`` is the model's own text or a
    deterministic fallback built because no round ever produced any.
    """

    def __init__(self, partial_content: str, narrative_synthesized: bool) -> None:
        super().__init__("sub-agent tool iteration limit reached")
        self.partial_content = partial_content
        self.narrative_synthesized = narrative_synthesized


def _build_capped_partial_content(state: "_ToolLoopState") -> tuple[str, bool]:
    """Build what a capped worker reports, and whether it is a synthesized fallback.

    ``state.round_texts`` holds every round's own assistant text, including the
    round that trips the cap — recovered here instead of trusting only that one
    round's ``response_content``, which is frequently empty when a tool-call round
    carries no accompanying text (FRE-1399: the same cap produced 1,677 characters
    in one real trace and 0 in another, purely because of whether that one round's
    completion happened to include text). Falls back to a deterministic,
    non-generated description when no round ever produced text — never a second
    inference call (FRE-1387 ruled that out for the digest cap; the same reasoning
    applies to this terminal path).

    Args:
        state: The tool loop's accumulator at the moment the cap fires.

    Returns:
        A tuple of (content to report, whether it is a synthesized fallback).
    """
    if state.round_texts:
        return "\n\n".join(state.round_texts), False
    return (
        f"[Reached the tool-iteration limit after {state.tool_iterations} round(s) "
        "of tool calls with no assistant text; absorbed "
        f"{state.tool_result_chars_absorbed} characters of tool output.]"
    ), True


async def _run_tool_loop(
    state: _ToolLoopState,
    spec: SubAgentSpec,
    llm_client: Any,
    tool_defs: list[dict[str, Any]] | None,
    tool_layer: Any,
    loaded_skills: set[str],
    trace_ctx: TraceContext,
    trace_id: str,
    session_id: str | None,
    effective_timeout: float,
    approval_required_tools: frozenset[str],
    deadline_monotonic: float,
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
        trace_ctx: The sub-agent's identity context, built once by
            :func:`run_sub_agent` (FRE-1467) and used for both the inference call
            and every tool dispatch. Passed in rather than constructed here, and
            constructed once rather than per use: two independent constructions
            can be threaded apart, which produces a tool that works in some
            rounds and not others. The object is a frozen dataclass whose
            ``span_id`` resolves live on access, so sharing one instance across
            every round is safe.
        trace_id: Parent request trace identifier. Kept alongside ``trace_ctx``
            because this function's log calls read it directly.
        session_id: Originating session id. Same reason as ``trace_id``.
        effective_timeout: The generation budget from
            :func:`_resolve_effective_timeout` — the same number the outer deadline is
            sized from. Passed explicitly rather than left for the client to re-derive
            (ADR-0145 D1 sketches the omit-and-re-derive form) because only the local
            branch re-derives it: the cloud branch applies no timeout at all when the
            call names none, so a cloud-placed ``sub_agent`` deployment would dispatch
            with no role budget. Handing back the number the client itself declared is
            not the "explicit override beats the declaration" trap this ticket removes
            — it IS the declaration — and it keeps every other cloud producer's
            timeout untouched.
        approval_required_tools: Granted tools that need the owner's word before
            they run (FRE-1461), resolved once by
            :func:`~personal_agent.orchestrator.sub_agent_approval.resolve_sub_agent_approval_requirements`.
            Empty for every sub-agent under today's shipped config, which is why
            this ships inert.
        deadline_monotonic: This call's absolute ``time.monotonic()`` deadline — the
            same bound the outer ``wait_for`` enforces. The approval pause is opened
            only when enough of it remains for the worker to outlive its own wait,
            so a refusal is always recorded rather than lost to a mid-pause kill.

    Raises:
        _ToolIterationLimitReached: When another tool batch would exceed
            ``settings.sub_agent_max_tool_iterations``.
    """
    tool_choice = "auto" if tool_defs else None
    while True:
        round_progress = GenerationProgress()
        state.progress = round_progress
        raw_response = await llm_client.respond(
            role=spec.model_role,
            messages=state.messages,
            max_tokens=spec.max_tokens,
            trace_ctx=trace_ctx,
            timeout_s=effective_timeout,
            progress_sink=round_progress,
            tools=tool_defs,
            tool_choice=tool_choice,
        )
        state.cost_usd += _extract_call_cost(raw_response)
        raw_tool_calls = _extract_tool_calls(raw_response)
        response_content = _parse_llm_response(raw_response)
        # FRE-1399: keep every round's own text, not just the round that ends up
        # tripping the cap below — whitespace-only content (" \n") counts as no
        # narrative, same as "".
        if response_content.strip():
            state.round_texts.append(response_content)

        if not raw_tool_calls:
            return _extract_stated_tool_gap(response_content)

        if state.tool_iterations >= settings.sub_agent_max_tool_iterations:
            partial_content, narrative_synthesized = _build_capped_partial_content(state)
            raise _ToolIterationLimitReached(partial_content, narrative_synthesized)

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

            # FRE-1461: the owner's gate. Checked AFTER the argument parse, so a
            # malformed call is refused without troubling anyone, and before
            # dispatch, so a denial costs nothing. A missing broker is a denial,
            # not an allowance: an approval-required tool with nobody to ask has no
            # other safe answer, and the pre-FRE-1461 behaviour here was to proceed
            # with only a warning logged.
            if tool_name in approval_required_tools:
                broker = get_sub_agent_approval_broker()
                if broker is None:
                    outcome_approved, outcome_reason = False, "no_approver_in_scope"
                else:
                    outcome = await broker.decide(
                        tool_name,
                        task=spec.task,
                        worker_remaining_seconds=deadline_monotonic - time.monotonic(),
                    )
                    outcome_approved, outcome_reason = outcome.approved, outcome.reason
                if not outcome_approved:
                    logger.warning(
                        "sub_agent_tool_approval_denied",
                        tool_name=tool_name,
                        reason=outcome_reason,
                        task=spec.task,
                        trace_id=trace_id,
                        session_id=session_id,
                    )
                    error_content = json.dumps(
                        {
                            "status": "error",
                            "hint": (
                                f"{tool_name} was not approved for this turn "
                                f"({outcome_reason}). Continue without it."
                            ),
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
                trace_ctx=trace_ctx,
                trace_id=trace_id,
                session_id=session_id,
                loaded_skills=loaded_skills,
                principal="sub_agent",
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
    user_id: UUID | None = None,
    authenticated: bool = False,
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
        user_id: The turn's owning user UUID (FRE-1467, ADR-0064). Reaches every
            granted tool through this call's one ``TraceContext``; see the module
            docstring for what each tool then returns that it did not before.
        authenticated: Whether the turn carries a verified identity (FRE-229 /
            FRE-673). Threads into the memory visibility filter.

    Returns:
        SubAgentResult with summary, metrics, and success status.

    Note:
        ``user_id`` and ``authenticated`` default to the unauthenticated pair, so
        a caller that does not supply them — a headless or background path —
        keeps producing a fail-closed context. The defaults must never be
        widened: they are what stops an unauthenticated turn reading
        ``group``-visibility memory.
    """
    # FRE-517: real UUID so it can key the (trace_id, task_id) route-trace segment row.
    # Stringified once for every wire/log/ES boundary; only SubAgentResult keeps the UUID.
    task_id = uuid.uuid4()
    task_id_str = str(task_id)
    start_ms = int(time.monotonic() * 1000)

    # FRE-1467: ONE context for this whole sub-agent — its inference call and
    # every tool it dispatches. Built here because this is the only place that
    # holds all five fields at once. Mirrors executor.run_task's own
    # construction field for field, which is what "the same identity the primary
    # carries" has to mean.
    trace_ctx = TraceContext(
        trace_id=trace_id,
        user_id=user_id,
        session_id=session_id,
        eval_mode=eval_mode,
        authenticated=authenticated,
    )

    # Build system prompt: base + optional skill index inherited from parent (Phase B).
    # Built before the try so the FRE-505 input-context breakdown is available on every
    # terminal path (success/timeout/exception/cancel), and so cancellation — which
    # raises BaseException, not Exception — can still emit an audit record.
    _system_content = _SUB_AGENT_SYSTEM_PROMPT
    if spec.skill_index_block:
        _system_content = f"{_system_content}\n\n{spec.skill_index_block}"
    _context_breakdown = _summarize_input_context(_system_content, spec)

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
        # ADR-0145 D1: resolved once, inside the audited region, and used twice — by the
        # outer net below and by the dispatched call itself. Inside the try because an
        # unconfigurable sub-agent must still produce an audit record and a reported
        # failure, not an escaping exception.
        effective_timeout = _resolve_effective_timeout(spec, llm_client)

        # Logged here rather than from the spec: `spec.timeout_seconds` is None on every
        # production spec now, so the spec reports the override, not the budget in force.
        logger.info(
            "sub_agent_start",
            task_id=task_id_str,
            task=spec.task,
            output_format=spec.output_format,
            max_tokens=spec.max_tokens,
            timeout=effective_timeout,
            trace_id=trace_id,
            session_id=session_id,
            **_context_breakdown,
        )

        # FRE-1374: timeout_s reaches the client as the GENERATION-only budget — for
        # LiteLLMClient it becomes the read timeout applied inside its concurrency-slot
        # context, so it starts counting at slot acquisition, not at spawn. The outer
        # wait_for below bounds the ENTIRE tool loop (every round's inference and tool
        # execution, FRE-1389) — a separate, larger, explicitly-named safety net (not
        # the primary timeout mechanism) for a client that ignores timeout_s. Scaled by
        # the iteration cap for a tool-granted spec (_effective_hard_deadline) — the
        # single-call sizing alone would kill a genuine multi-round loop before it ever
        # reached its own cap.
        hard_deadline = _effective_hard_deadline(spec, effective_timeout)
        if max_deadline_seconds is not None:
            hard_deadline = min(hard_deadline, max_deadline_seconds)

        # FRE-1461: resolved once per sub-agent, not per tool call — the governance
        # config is read from YAML each time, and the answer cannot change inside one
        # worker's loop in any way that matters. An empty grant costs no lookup.
        approval_required_tools = resolve_sub_agent_approval_requirements(
            spec.tools, trace_id=trace_id
        )
        # The same instant the wait_for below is measured from, expressed absolutely
        # so the approval gate can ask how much of THIS worker's budget is left.
        deadline_monotonic = time.monotonic() + hard_deadline

        response_content, stated_tool_gap = await asyncio.wait_for(
            _run_tool_loop(
                state,
                spec,
                llm_client,
                tool_defs,
                tool_layer,
                loaded_skills,
                trace_ctx,
                trace_id,
                session_id,
                effective_timeout,
                approval_required_tools,
                deadline_monotonic,
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
            narrative_synthesized=exc.narrative_synthesized,
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
    _warn_if_narrative_synthesized(result, trace_id, session_id)

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
