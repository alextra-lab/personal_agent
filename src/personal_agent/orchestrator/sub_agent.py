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
import inspect
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog

from personal_agent.captains_log.capture import SubAgentCapture, write_sub_agent_capture
from personal_agent.config import settings
from personal_agent.llm_client.models import Dialect, synthesis_retains_tools
from personal_agent.llm_client.types import GenerationProgress, LLMTimeout
from personal_agent.orchestrator.prompts import render_current_datetime_block
from personal_agent.orchestrator.sub_agent_approval import (
    get_sub_agent_approval_broker,
    resolve_sub_agent_approval_requirements,
)
from personal_agent.orchestrator.sub_agent_types import (
    SubAgentReportKind,
    SubAgentResult,
    SubAgentSpec,
    SubAgentStopReason,
)
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

# ADR-0149 D3 move 1: the worker is told its budget before it spends any of it.
# Rendered from the setting rather than hardcoded — FRE-1389 AC-1 already ruled a
# hardcoded number out for the tool surface, and the same drift applies here.
_BUDGET_BLOCK_TEMPLATE = (
    "You have a budget of {n} tool round(s). A round is one reply that calls tools; "
    "it may call several tools in parallel. After each round you will be told how "
    "much budget remains. When the budget is spent, your next reply will have no "
    "tools available, and you must write your report from the results you already hold."
)

# ADR-0149 D3 move 3: appended after every round's tool results, at the tail only.
# Characters are reported alongside rounds because context growth is what actually
# binds (D1), even though the enforced cap stays round-denominated.
_COUNTDOWN_TEMPLATE = (
    "Tool budget: {remaining} of {n} round(s) remaining. Absorbed so far: "
    "{chars:,} characters of tool output. Time remaining: about {seconds} s."
)

# ADR-0149 D3 move 5: the forced-synthesis instruction. The text is advice; the
# absence of callable tools is the enforcement.
_SYNTHESIS_INSTRUCTION = (
    "{opening} Do NOT call any more tools. Using only the tool results already in "
    "this conversation, write your report now. State every fact you found that "
    "answers the task, each with the source it came from. Then list what you "
    "searched for and did not find. Keep the report under 400 words. "
    "Output format: {output_format}."
)
_SYNTHESIS_OPENING_CAP = "Your tool budget is spent."
_SYNTHESIS_OPENING_RESERVE = "Your time budget is nearly spent."

# Bound on the tool arguments the ledger reproduces, per call. The ledger exists so
# the primary can re-run a query, so the arguments must survive; but they are
# model-authored and a `run_python` body has no natural size. 1,000 characters is far
# above any real search query, and a clipped value says so rather than looking whole.
_LEDGER_ARGS_CAP_CHARS = 1_000


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
    # `+ 1` for the forced-synthesis call (ADR-0149 D3 move 5). Before it, a capped
    # worker's last inference WAS one of the N rounds, so N budgets sized the loop
    # exactly. Now the loop makes at most N rounds AND one call that writes the
    # report, so a net sized at N would be guaranteed to cut the very call this
    # ADR adds — and the landing reserve, which asks for `mean_round + one budget`
    # of headroom, would fire before the first round rather than near the end.
    # This is the derived safety net, not one of the three values AC-8 protects:
    # the round cap, the role's generation budget and the turn budget are
    # unchanged, and `max_deadline_seconds` still shrinks this via min() so a
    # fan-out can never outlive its turn.
    return max(
        single_call_deadline,
        effective_timeout * (settings.sub_agent_max_tool_iterations + 1),
    )


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
    rounds: list[dict[str, Any]] | None = None,
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
        rounds: Per-tool-call record — round, tool, argument size, result size and
            the round's wall-clock (ADR-0149 D5). This is the evidence a proposal
            to raise any of the three limits must cite; a proposal without it
            fails that ADR's own bar. ``None`` normalizes to an empty list.
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
        stop_reason=result.stop_reason,
        report_kind=result.report_kind,
        rounds=rounds if rounds is not None else [],
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


def _resolve_synthesis_dialect(llm_client: Any, role: Any) -> "Dialect | None":
    """Resolve the client's dialect for the forced-synthesis call, or ``None``.

    Defensive on purpose. ``llm_client`` is typed ``Any`` throughout this module
    and is not always a ``LiteLLMClient``: the PARALLEL_INFERENCE path and every
    test double pass something else, and an ``AsyncMock`` answers *any* attribute
    with a coroutine factory. Handing that straight to
    :func:`~personal_agent.llm_client.models.synthesis_retains_tools` raises a
    ``KeyError`` out of the worker, turning a capped worker into a crashed one —
    the exact opposite of what ADR-0149 is for.

    Anything that is not a real :class:`Dialect` resolves to ``None``, which
    :func:`synthesis_retains_tools` turns into the cache-preserving form with a
    WARNING. That is the safe direction: keeping the tools costs nothing on a
    provider that would have tolerated either form.

    Args:
        llm_client: The client this sub-agent dispatches through.
        role: The model role this worker runs as.

    Returns:
        The resolved dialect, or ``None`` when the client does not implement the
        seam, raises, or answers with something that is not a dialect.
    """
    resolver = getattr(llm_client, "dialect_for_role", None)
    if resolver is None:
        return None
    try:
        resolved = resolver(role)
    except Exception as exc:
        logger.warning("synthesis_dialect_lookup_failed", error=str(exc))
        return None
    if isinstance(resolved, Dialect):
        return resolved
    if inspect.iscoroutine(resolved):
        # Close it rather than leave an un-awaited coroutine behind.
        resolved.close()
    return None


def _terminal_error(outcome: "_ToolLoopOutcome", state: "_ToolLoopState") -> str:
    """Describe a non-successful loop outcome for ``SubAgentResult.error``.

    Args:
        outcome: The loop's terminal outcome.
        state: The loop accumulator, for the round count.

    Returns:
        A short human-readable reason naming the path and what was reported.
    """
    if outcome.stop_reason == "cap":
        return (
            f"tool iteration limit reached after {state.tool_iterations} rounds "
            f"(report: {outcome.report_kind})"
        )
    return f"stopped: {outcome.stop_reason} (report: {outcome.report_kind})"


def _build_sub_agent_system_prompt(skill_index_block: str) -> str:
    """Build the worker's system prompt, including its round budget (ADR-0149 move 1).

    The budget paragraph is rendered from ``settings.sub_agent_max_tool_iterations``
    rather than written as a literal, for the reason FRE-1389 AC-1 gave for the tool
    surface: a hardcoded number drifts away from the value that actually binds, and
    nothing detects it.

    It is rendered for every worker, including one holding no tools. A grant-less
    worker still runs the same loop, and AC-4a requires the bytes to be identical
    across the workers of one fan-out — which can mix granted and grant-less tasks.

    Args:
        skill_index_block: The parent's compact skill index, appended when present.

    Returns:
        The complete system prompt.
    """
    parts = [
        _SUB_AGENT_SYSTEM_PROMPT,
        _BUDGET_BLOCK_TEMPLATE.format(n=settings.sub_agent_max_tool_iterations),
    ]
    if skill_index_block:
        parts.append(skill_index_block)
    return "\n\n".join(parts)


def _build_task_message(spec: SubAgentSpec, trace_id: str, session_id: str | None) -> str:
    """Build the worker's task user message, with the turn's date (ADR-0149 move 2).

    The date lives here rather than in the system prompt because the prompt is one
    shared, byte-identical string and a date would change it on every turn. On
    2026-09-10 a worker with no date spent all five of its rounds searching 2025
    events, against a question about 2026.

    Args:
        spec: The sub-agent specification.
        trace_id: Parent request trace identifier.
        session_id: Originating session id.

    Returns:
        The task message content.
    """
    body = f"Task: {spec.task}\nOutput format: {spec.output_format}\nRespond with the result only."
    if spec.turn_started_at is None:
        # A caller outside a turn. The block is omitted rather than a date being
        # invented from `now`, which would silently differ from the turn's own
        # instant on every other model call this turn makes.
        logger.warning(
            "sub_agent_no_turn_timestamp",
            task=spec.task,
            trace_id=trace_id,
            session_id=session_id,
        )
        return body
    return f"{render_current_datetime_block(spec.turn_started_at)}\n\n{body}"


def _killed_result(
    task_id: uuid.UUID,
    spec: SubAgentSpec,
    duration_ms: float,
    state: "_ToolLoopState",
    error: str,
    stop_reason: SubAgentStopReason,
    why: str,
) -> SubAgentResult:
    """Build a SubAgentResult for a sub-agent that never returned (FRE-1379).

    FRE-1379 recovered the streaming client's in-flight fragment here, so a
    killed worker stopped reporting the empty ``digest_chars=0,
    full_output_chars=0`` record. ADR-0149 finds that insufficient and replaces
    the fragment with the ledger: on 2026-09-10 two OVH workers died on the
    90 s per-call timeout after 6 and 15 successful searches and reported zero
    characters each, because the fragment was empty — the call that died was a
    *tool* call, not a writing call, so there was nothing to recover.

    The fragment is not discarded. It is quoted inside the ledger, under
    "Partial text from the interrupted call", alongside the queries the worker
    made and the sizes of what they returned.

    Shared by the outer hard-deadline timeout, a global dispatch cancellation,
    and any unexpected exception — all the same "killed with whatever state was
    accumulated" shape, just different triggers.

    Args:
        task_id: This sub-agent invocation's identifier.
        spec: The sub-agent specification.
        duration_ms: Wall-clock time since spawn.
        state: The loop accumulator, holding every completed round's activity.
        error: Human-readable reason, distinguishing which budget fired.
        stop_reason: The declared terminal path.
        why: A short clause completing "no model-written report was possible
            because …".

    Returns:
        A failed SubAgentResult whose content is the ledger.
    """
    partial = state.progress.content
    ledger = _build_ledger(state, stop_reason, why, partial)
    elapsed_generation_ms = (
        (time.monotonic() - state.progress.generation_started_monotonic) * 1000
        if state.progress.generation_started_monotonic is not None
        else None
    )
    return SubAgentResult(
        task_id=task_id,
        spec_task=spec.task,
        summary=ledger[:_SUMMARY_CAP_CHARS],
        full_output=ledger,
        tools_used=state.tools_used,
        token_count=0,
        # From what the MODEL generated, never from the ledger: the ledger is
        # deterministic text this function wrote, and counting it as generation
        # would inflate every killed worker's figure.
        tokens_generated=len(partial.split()) if partial else 0,
        elapsed_generation_ms=elapsed_generation_ms,
        duration_ms=duration_ms,
        success=False,
        error=error,
        cost_usd=state.cost_usd,
        denied_tools=spec.denied_tools,
        tool_iterations=state.tool_iterations,
        tool_result_chars_absorbed=state.tool_result_chars_absorbed,
        refused_tool_attempts=tuple(dict.fromkeys(state.refused_tool_attempts)),
        stop_reason=stop_reason,
        report_kind="ledger",
        narrative_synthesized=True,
    )


@dataclass(frozen=True)
class _ToolCallRecord:
    """One dispatched, refused or malformed tool call, for the ledger (ADR-0149 D3).

    Holds the call's arguments and the SIZE of its result, never the result body.
    That asymmetry is the point: the queries are what make a ledger actionable —
    the primary can re-run one — while the raw results are exactly what context
    isolation exists to keep out of the parent's synthesis context (FRE-1389 AC-4).

    Attributes:
        round_num: The 1-based round this call belonged to.
        tool: The tool name the model asked for, whether or not it was dispatched.
        arguments: The raw argument string, clipped at
            :data:`_LEDGER_ARGS_CAP_CHARS` with an explicit marker.
        result_chars: Length of the tool-role content fed back to the model.
        wall_s: Wall-clock of the whole round this call belonged to, back-filled
            when the round closes. ``None`` while the round is still open, which
            is how a kill mid-round is distinguishable from a completed one.
    """

    round_num: int
    tool: str
    arguments: str
    result_chars: int
    wall_s: float | None = None


@dataclass
class _ToolLoopState:
    """Mutable tool-loop accumulator, external to the loop coroutine's own frame.

    A cancellation mid-loop (the outer hard-deadline ``wait_for``, FRE-1379)
    destroys the cancelled coroutine's local frame — exactly why
    ``GenerationProgress`` exists for the single-call case. This extends the
    same pattern across a multi-round loop (FRE-1389): every completed
    round's activity lands here as it happens, so a kill mid-round still
    reports every prior round's cost/tools/chars, not just the in-flight one.
    ADR-0149 extends it once more, to the per-round record the ledger is built
    from and the per-round wall-clock the landing reserve is sized against.
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
    # ADR-0149 D3: the ledger's raw material, and the reserve's clock.
    tool_calls: list[_ToolCallRecord] = field(default_factory=list)
    round_wall_s: list[float] = field(default_factory=list)

    def mean_round_s(self, fallback: float) -> float:
        """Mean wall-clock of the rounds completed so far in this worker.

        Args:
            fallback: The conservative estimate to use before any round has
                completed — the generation budget, which is the most a single
                round's inference can cost.

        Returns:
            The measured mean, or ``fallback`` when nothing is measured yet.
        """
        if not self.round_wall_s:
            return fallback
        return sum(self.round_wall_s) / len(self.round_wall_s)

    def close_round(self, wall_s: float) -> None:
        """Record a completed round's wall-clock and stamp it onto that round's calls.

        Args:
            wall_s: Wall-clock of the round, from the start of its inference call
                to the end of its tool execution.
        """
        self.round_wall_s.append(wall_s)
        self.tool_calls[:] = [
            replace(record, wall_s=wall_s)
            if record.round_num == self.tool_iterations and record.wall_s is None
            else record
            for record in self.tool_calls
        ]

    def capture_rounds(self) -> list[dict[str, Any]]:
        """The per-call record in the shape the audit record stores (ADR-0149 D5).

        Returns:
            One mapping per tool call: round number, tool name, argument size,
            result size and the round's wall-clock. Argument *sizes*, not the
            arguments themselves — the capture is a metrics surface, and a raise
            proposal reads it in aggregate.
        """
        return [
            {
                "round": record.round_num,
                "tool": record.tool,
                "args_chars": len(record.arguments),
                "result_chars": record.result_chars,
                "wall_s": record.wall_s,
            }
            for record in self.tool_calls
        ]


@dataclass(frozen=True)
class _ToolLoopOutcome:
    """What the tool loop returns on every terminal path (ADR-0149 D3).

    Replaces the former ``_ToolIterationLimitReached`` exception. FRE-1389 made the
    cap a raise because the worker was "a pure bounded function: no injected 'please
    wrap up' round, just a stop". ADR-0149 reverses that with the reason stated: a
    pure function that returns 57 characters against 156,749 absorbed is not
    measurable either, and the one thing FRE-1389 left out is that the function must
    return its result. The cap is now an ordinary return carrying a report.

    Attributes:
        content: The terminal content — a model-written report, a cut partial
            followed by the ledger, or the ledger alone.
        stated_tool_gap: A ``TOOL_GAP:`` name stripped from a completed reply.
        stop_reason: Why the loop ended.
        report_kind: What ``content`` is.
    """

    content: str
    stated_tool_gap: str | None
    stop_reason: SubAgentStopReason
    report_kind: SubAgentReportKind


def _clip(text: str, cap: int) -> str:
    """Clip text to a cap, marking the clip so it never reads as complete.

    Args:
        text: The text to bound.
        cap: Maximum characters to keep.

    Returns:
        The text unchanged when it fits, else the first ``cap`` characters with a
        marker naming how many were dropped.
    """
    if len(text) <= cap:
        return text
    return f"{text[:cap]}…[truncated {len(text) - cap} chars]"


def _build_ledger(
    state: _ToolLoopState,
    stop_reason: SubAgentStopReason,
    why: str,
    partial_text: str = "",
) -> str:
    """Build the deterministic account of a worker that could not write a report.

    No inference, no free-text parsing — assembled from :class:`_ToolLoopState`
    alone, so it is available on every path including one that was cancelled out
    from under the loop. It replaces the bare in-flight fragment that every
    killed worker used to return, and which on 2026-09-10 was zero characters on
    both OVH failures after real, billed research.

    What it carries and what it deliberately does not: the tool calls with their
    arguments and result SIZES, so the primary can re-run a query; never the
    results themselves, which is what context isolation exists to prevent
    (FRE-1389 AC-4).

    Args:
        state: The loop accumulator at the moment the path terminated.
        stop_reason: The declared terminal path.
        why: A short clause completing "no model-written report was possible
            because …".
        partial_text: Streamed text from the interrupted call, if any.

    Returns:
        The ledger text.
    """
    lines = [
        f"[Worker stopped: {stop_reason} after {state.tool_iterations} tool round(s); "
        f"absorbed {state.tool_result_chars_absorbed:,} characters of tool output; "
        f"no model-written report was possible because {why}.]"
    ]

    if state.tool_calls:
        lines.append("Tool calls made:")
        lines.extend(
            f"  {idx}. {record.tool}({record.arguments}) → {record.result_chars:,} chars"
            for idx, record in enumerate(state.tool_calls, start=1)
        )

    if state.round_texts:
        lines.append("Model notes per round:")
        lines.extend(f"  - round {idx}: {text!r}" for idx, text in enumerate(state.round_texts, 1))

    if partial_text.strip():
        # Never clipped here. ADR-0149's own instructive case is worker 4 of
        # session 606ae6a4: cut mid-generation, it still returned 7,203 useful
        # characters, because the call that died was a WRITING call. A local cap
        # would discard most of that. `_SUMMARY_CAP_CHARS` is the circuit breaker
        # for a runaway, and it is sized for exactly this job.
        lines.append("Partial text from the interrupted call:")
        lines.append(f"  {partial_text!r}")

    return "\n".join(lines)


async def _forced_synthesis(
    state: _ToolLoopState,
    spec: SubAgentSpec,
    llm_client: Any,
    tool_defs: list[dict[str, Any]] | None,
    trace_ctx: TraceContext,
    trace_id: str,
    session_id: str | None,
    effective_timeout: float,
    deadline_monotonic: float,
    stop_reason: SubAgentStopReason,
) -> _ToolLoopOutcome:
    """Make the one tools-off call that turns absorbed results into a report.

    ADR-0149 D3 move 5. Today the call after the last executed round emits tool
    calls that are discarded; after this it writes the report instead, so the
    number of inference calls a capped worker makes is unchanged and the last one
    is useful. On 2026-09-10 a worker reached its cap holding 154,755 characters
    that contained the answer and reported five stage directions.

    The call keeps its ``tools`` array and pins ``tool_choice="none"`` wherever the
    resolved dialect declares that form (D6). Dropping the array would re-render
    the whole prefix and lose the cache on the largest prefix the worker will ever
    hold — which on the OVH path, already dying at a 90 s per-call timeout, would
    have made this ticket's own failure mode more frequent, not less.

    Exactly one attempt is made. A provider that rejects ``tool_choice="none"``
    despite declaring it is a configuration defect, and the worker reports a
    ledger naming the provider and dialect rather than retrying without tools:
    the worker recovers from its own limits, it does not retry the world.

    Args:
        state: The loop accumulator; its message list is appended to.
        spec: The sub-agent specification.
        llm_client: LLM client instance.
        tool_defs: The worker's granted tool definitions, retained on the call
            when the dialect declares the cache-preserving form.
        trace_ctx: The sub-agent's identity context.
        trace_id: Parent request trace identifier.
        session_id: Originating session id.
        effective_timeout: The generation budget.
        deadline_monotonic: This worker's absolute deadline.
        stop_reason: The path that triggered this call, carried onto the outcome.

    Returns:
        The terminal outcome: a ``synthesized`` report, a ``narration`` partial
        followed by the ledger, or the ledger alone.
    """
    # Recomputed here, never inherited: on the timeout path the caller's own
    # figure is stale by a whole generation budget.
    remaining_s = deadline_monotonic - time.monotonic()
    if remaining_s <= 0:
        # The triggering path keeps its own name. A per-call timeout with nothing
        # left is still `timeout`; `deadline` belongs to the outer wait_for alone.
        return _ToolLoopOutcome(
            content=_build_ledger(
                state, stop_reason, "no time remained to write one", state.progress.content
            ),
            stated_tool_gap=None,
            stop_reason=stop_reason,
            report_kind="ledger",
        )

    opening = (
        _SYNTHESIS_OPENING_RESERVE if stop_reason == "time_reserve" else _SYNTHESIS_OPENING_CAP
    )
    state.messages.append(
        {
            "role": "user",
            "content": _SYNTHESIS_INSTRUCTION.format(
                opening=opening, output_format=spec.output_format
            ),
        }
    )

    dialect = _resolve_synthesis_dialect(llm_client, spec.model_role)
    retains_tools = synthesis_retains_tools(dialect)
    if not retains_tools:
        logger.warning(
            "forced_synthesis_cache_miss_declared",
            dialect=getattr(dialect, "value", None),
            provider=getattr(llm_client, "provider", None),
            task=spec.task,
            trace_id=trace_id,
            session_id=session_id,
        )

    # Its own sink: a cut synthesis call's streamed partial IS the report
    # (ADR-0149 D3 move 5), and the round sink holds the previous round's text.
    synthesis_progress = GenerationProgress()
    state.progress = synthesis_progress
    logger.info(
        "sub_agent_forced_synthesis",
        stop_reason=stop_reason,
        retains_tools=retains_tools,
        dialect=getattr(dialect, "value", None),
        tool_iterations=state.tool_iterations,
        tool_result_chars_absorbed=state.tool_result_chars_absorbed,
        trace_id=trace_id,
        session_id=session_id,
    )
    try:
        raw_response = await llm_client.respond(
            role=spec.model_role,
            messages=state.messages,
            max_tokens=spec.max_tokens,
            trace_ctx=trace_ctx,
            timeout_s=min(effective_timeout, remaining_s),
            progress_sink=synthesis_progress,
            tools=tool_defs if retains_tools else None,
            tool_choice="none" if retains_tools and tool_defs else None,
        )
    # `Exception`, never `BaseException`: a CancelledError raised inside this call
    # must reach run_sub_agent's cancel handler, which writes the audit record and
    # re-raises. Swallowing it here would turn a cancelled turn into a result.
    except Exception as exc:
        partial = synthesis_progress.content
        why = (
            f"the synthesis call failed on provider "
            f"{getattr(llm_client, 'provider', None)!r} "
            f"(dialect {getattr(dialect, 'value', None)!r}): {exc}"
        )
        if partial.strip():
            return _ToolLoopOutcome(
                content=f"{partial}\n\n{_build_ledger(state, stop_reason, why)}",
                stated_tool_gap=None,
                stop_reason=stop_reason,
                report_kind="narration",
            )
        return _ToolLoopOutcome(
            content=_build_ledger(state, stop_reason, why),
            stated_tool_gap=None,
            stop_reason=stop_reason,
            report_kind="ledger",
        )

    state.cost_usd += _extract_call_cost(raw_response)
    content, stated_tool_gap = _extract_stated_tool_gap(_parse_llm_response(raw_response))
    if content.strip():
        return _ToolLoopOutcome(
            content=content,
            stated_tool_gap=stated_tool_gap,
            stop_reason=stop_reason,
            report_kind="synthesized",
        )
    return _ToolLoopOutcome(
        content=_build_ledger(state, stop_reason, "the synthesis call returned no text"),
        stated_tool_gap=stated_tool_gap,
        stop_reason=stop_reason,
        report_kind="ledger",
    )


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
) -> _ToolLoopOutcome:
    """Run inference/tool-execution rounds until the model stops or a limit fires.

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

    Returns:
        The terminal :class:`_ToolLoopOutcome`, declaring both why the loop ended
        and what kind of report its content is. Every exit is a return: the cap
        used to raise (FRE-1389's "just a stop"), and ADR-0149 reverses that so
        the bounded function returns its result.
    """
    tool_choice = "auto" if tool_defs else None
    max_iterations = settings.sub_agent_max_tool_iterations
    while True:
        # ADR-0149 D3 move 5, FIRST — before any inference call. The primary's
        # loop checks its cap AFTER the call, so the model spends one whole
        # generation emitting tool calls that are then dropped, and only then
        # does the tools-off call follow (ADR-0149 D1 defect 2). On a five-round
        # budget that is a sixth of the loop, and on OVH it is the call that dies
        # at 90 s. Checking here makes the sixth call the report instead.
        # Ungated by `tool_defs` deliberately. A grant-less worker should never
        # reach a second round, but the cap is also the only thing bounding a
        # model that emits tool calls it was never offered — every one is refused
        # below, and without this check the loop would refuse them forever.
        if state.tool_iterations >= max_iterations:
            return await _forced_synthesis(
                state,
                spec,
                llm_client,
                tool_defs,
                trace_ctx,
                trace_id,
                session_id,
                effective_timeout,
                deadline_monotonic,
                stop_reason="cap",
            )

        # ADR-0149 D3 move 4: reserve the landing. Gated on `tool_defs` — a
        # grant-less worker makes exactly one call, and its hard deadline is sized
        # for one call (`_effective_hard_deadline`), so the reserve would fire on
        # every such worker and replace its only real call with a synthesis call
        # holding nothing to synthesise from.
        if tool_defs:
            remaining_s = deadline_monotonic - time.monotonic()
            mean_round_s = state.mean_round_s(effective_timeout)
            if remaining_s < mean_round_s + effective_timeout:
                logger.info(
                    "sub_agent_landing_reserved",
                    remaining_s=round(remaining_s, 2),
                    mean_round_s=round(mean_round_s, 2),
                    effective_timeout=effective_timeout,
                    tool_iterations=state.tool_iterations,
                    trace_id=trace_id,
                    session_id=session_id,
                )
                return await _forced_synthesis(
                    state,
                    spec,
                    llm_client,
                    tool_defs,
                    trace_ctx,
                    trace_id,
                    session_id,
                    effective_timeout,
                    deadline_monotonic,
                    stop_reason="time_reserve",
                )

        round_started = time.monotonic()
        round_progress = GenerationProgress()
        state.progress = round_progress
        try:
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
        except LLMTimeout as exc:
            # The generation budget fired on a tool round. One synthesis attempt
            # if any time remains — _forced_synthesis recomputes that itself and
            # returns a ledger when it does not. Before ADR-0149 this path went
            # straight to _killed_result and returned zero characters, which is
            # what both OVH workers did on 2026-09-10 after billed research.
            if state.round_texts or state.tool_calls:
                logger.warning(
                    "sub_agent_round_timeout_attempting_synthesis",
                    error=str(exc),
                    tool_iterations=state.tool_iterations,
                    trace_id=trace_id,
                    session_id=session_id,
                )
            return await _forced_synthesis(
                state,
                spec,
                llm_client,
                tool_defs,
                trace_ctx,
                trace_id,
                session_id,
                effective_timeout,
                deadline_monotonic,
                stop_reason="timeout",
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
            content, stated_tool_gap = _extract_stated_tool_gap(response_content)
            if content.strip():
                return _ToolLoopOutcome(
                    content=content,
                    stated_tool_gap=stated_tool_gap,
                    stop_reason="completed",
                    report_kind="synthesized",
                )
            # ADR-0149 D3: empty content is not a report. The worker stopped of
            # its own accord and said nothing, which reaches the caller as a
            # failed landing rather than a success carrying an empty digest.
            return _ToolLoopOutcome(
                content=_build_ledger(state, "completed", "the model returned no text"),
                stated_tool_gap=stated_tool_gap,
                stop_reason="completed",
                report_kind="ledger",
            )

        state.tool_iterations += 1
        normalized_calls = _normalize_tool_calls(raw_tool_calls, state.tool_iterations)
        state.messages.append(
            {"role": "assistant", "content": response_content, "tool_calls": normalized_calls}
        )

        def _absorb(tool_call_id: str, tool_name: str, raw_arguments: str, content: str) -> None:
            """Feed one tool result back to the model and record it for the ledger.

            Every tool-role message the loop appends goes through here — a real
            dispatch, an out-of-grant refusal, a malformed-argument error and an
            approval denial alike. All four are context the worker absorbed, and
            all four are things the primary may want to see it tried.

            Args:
                tool_call_id: The normalized call id this result answers.
                tool_name: The tool the model asked for.
                raw_arguments: The model's raw argument string.
                content: The tool-role content fed back.
            """
            state.tool_result_chars_absorbed += len(content)
            state.tool_calls.append(
                _ToolCallRecord(
                    round_num=state.tool_iterations,
                    tool=tool_name,
                    arguments=_clip(raw_arguments, _LEDGER_ARGS_CAP_CHARS),
                    result_chars=len(content),
                )
            )
            state.messages.append(
                {
                    "tool_call_id": tool_call_id,
                    "role": "tool",
                    "name": tool_name,
                    "content": content,
                }
            )

        for call, raw_call in zip(normalized_calls, raw_tool_calls, strict=True):
            tool_call_id = call["id"]
            tool_name = call["function"]["name"]
            raw_arguments = str(raw_call.get("arguments") or "{}")

            if tool_name not in spec.tools:
                if len(state.refused_tool_attempts) < _MAX_REFUSED_TOOL_ATTEMPTS:
                    state.refused_tool_attempts.append(tool_name)
                _absorb(
                    tool_call_id,
                    tool_name,
                    raw_arguments,
                    json.dumps(
                        {
                            "status": "error",
                            "hint": f"{tool_name} is not available to this sub-agent.",
                        }
                    ),
                )
                continue

            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                _absorb(
                    tool_call_id,
                    tool_name,
                    raw_arguments,
                    json.dumps(
                        {
                            "status": "retry",
                            "hint": (
                                "Arguments were not valid JSON. Retry with valid JSON arguments."
                            ),
                        }
                    ),
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
                    _absorb(
                        tool_call_id,
                        tool_name,
                        raw_arguments,
                        json.dumps(
                            {
                                "status": "error",
                                "hint": (
                                    f"{tool_name} was not approved for this turn "
                                    f"({outcome_reason}). Continue without it."
                                ),
                            }
                        ),
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
            _absorb(tool_call_id, tool_name, raw_arguments, str(dispatch_result["content"]))

        # The round is closed only here, after its tool execution — so the mean the
        # landing reserve reads is the real cost of a round, inference plus tools,
        # and a kill mid-round leaves this round's calls with wall_s None rather
        # than a figure that was never measured.
        state.close_round(time.monotonic() - round_started)

        # ADR-0149 D3 move 3: the countdown, appended after the round's tool
        # results and nowhere else. Every injection is a tail append, so nothing
        # above it changes and the prefix stays cacheable (ADR-0081 D2).
        state.messages.append(
            {
                "role": "user",
                "content": _COUNTDOWN_TEMPLATE.format(
                    remaining=max(max_iterations - state.tool_iterations, 0),
                    n=max_iterations,
                    chars=state.tool_result_chars_absorbed,
                    seconds=max(int(deadline_monotonic - time.monotonic()), 0),
                ),
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
    _system_content = _build_sub_agent_system_prompt(spec.skill_index_block)
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
    messages.append({"role": "user", "content": _build_task_message(spec, trace_id, session_id)})
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

        outcome = await asyncio.wait_for(
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

        # ADR-0149 D3: `success` reads `report_kind`, not "the content is
        # non-empty". The ledger is always non-empty, so a content test alone
        # would report a failed landing as a success. A worker that stopped at
        # its cap and wrote a good partial report is still not a success: it did
        # not finish its task, and FRE-1389 AC-2's distinct terminal state holds
        # for the new paths as it did for the cap.
        succeeded = outcome.stop_reason == "completed" and outcome.report_kind == "synthesized"
        result = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary=outcome.content[:_SUMMARY_CAP_CHARS],
            full_output=outcome.content,
            tools_used=state.tools_used,
            token_count=len(outcome.content.split()),
            tokens_generated=len(outcome.content.split()),
            elapsed_generation_ms=elapsed_generation_ms,
            duration_ms=duration_ms,
            success=succeeded,
            error=None if succeeded else _terminal_error(outcome, state),
            cost_usd=state.cost_usd,
            denied_tools=spec.denied_tools,
            tool_iterations=state.tool_iterations,
            tool_result_chars_absorbed=state.tool_result_chars_absorbed,
            refused_tool_attempts=tuple(dict.fromkeys(state.refused_tool_attempts)),
            stated_tool_gap=outcome.stated_tool_gap,
            stop_reason=outcome.stop_reason,
            report_kind=outcome.report_kind,
            # FRE-1399's flag, on the condition ADR-0149 D3 restates for it: the
            # worker did work and no model text survived to describe it.
            narrative_synthesized=outcome.report_kind == "ledger",
        )

    except asyncio.TimeoutError:
        # The outer wait_for. By definition no time remains, so there is no
        # synthesis attempt here — the ledger is the whole obligation.
        duration_ms = int(time.monotonic() * 1000) - start_ms
        result = _killed_result(
            task_id,
            spec,
            duration_ms,
            state,
            # FRE-1374 (AC-2): report the measured elapsed time, not the nominal
            # budget — the hard deadline that actually fired may differ from
            # spec.timeout_seconds, and the old hard-coded value hid exactly the
            # shortfall this ticket exists to make visible.
            error=f"Timeout after {duration_ms / 1000:.1f}s",
            stop_reason="deadline",
            why="the worker's outer deadline fired with no time left to write",
        )

    except LLMTimeout as exc:
        # FRE-1379: the client's own wall-clock generation budget fired. Since
        # ADR-0149 the loop catches this itself and attempts one synthesis, so
        # reaching here means the SYNTHESIS call was the one that timed out —
        # its own partial is in state.progress and the ledger quotes it.
        duration_ms = int(time.monotonic() * 1000) - start_ms
        result = _killed_result(
            task_id,
            spec,
            duration_ms,
            state,
            error=f"Timeout after {duration_ms / 1000:.1f}s (generation budget): {exc}",
            stop_reason="timeout",
            why="the generation budget fired while the report was being written",
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
            state,
            error="cancelled (global dispatch timeout)",
            stop_reason="cancelled",
            why="the dispatcher cancelled the worker",
        )
        # ADR-0149 D3: the ledger goes to the CAPTURE and the cancellation is
        # re-raised. No result reaches _run_dispatch, which catches only
        # Exception — and a global cancel ends the turn, so no caller could use
        # one anyway. The obligation on this path is the audit record.
        _emit_sub_agent_capture(
            cancelled,
            spec,
            _context_breakdown,
            trace_id,
            session_id,
            eval_mode,
            rounds=state.capture_rounds(),
        )
        _warn_if_clipped(cancelled, trace_id, session_id)
        raise

    except Exception as exc:
        duration_ms = int(time.monotonic() * 1000) - start_ms
        result = _killed_result(
            task_id,
            spec,
            duration_ms,
            state,
            error=str(exc),
            stop_reason="error",
            why=f"the worker raised before it could report: {exc}",
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
    _emit_sub_agent_capture(
        result,
        spec,
        _context_breakdown,
        trace_id,
        session_id,
        eval_mode,
        rounds=state.capture_rounds(),
    )
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
