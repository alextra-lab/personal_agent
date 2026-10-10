"""Expansion controller — deterministic workflow enforcement.

When the gateway sets strategy ∈ {HYBRID, DECOMPOSE}, this controller takes
over from the executor (FRE-1381: the only path — the autonomous alternative,
where the LLM decided whether to expand, was deleted). The LLM generates
plan content only; it does not decide whether to expand.

State machine:
  Gateway output → LLM planner → Plan validation → Executor dispatch
  → Partial aggregation → Synthesis → Final response

Fallback: If the LLM planner fails (invalid output, timeout, empty plan),
a deterministic fallback planner generates the plan.

See: ADR-0036 (expansion-controller)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from personal_agent.brainstem import ModeManagerError, get_current_mode
from personal_agent.config import GovernanceConfigError, get_settings, load_governance_config
from personal_agent.exceptions import PlannerInputTooLargeError
from personal_agent.governance.sub_agent_tools import (
    SUB_AGENT_DENIED_MODES,
    SubAgentToolGrant,
    evaluate_sub_agent_tool_grant,
)
from personal_agent.llm_client.message_content import get_text_content
from personal_agent.llm_client.types import ModelRole
from personal_agent.observability.route_trace.types import PlannerDecision, PlannerFailureReason
from personal_agent.observability.topology import report_degradation
from personal_agent.orchestrator.expansion_types import (
    SKIP_REASON_TEXT,
    ExpansionPhase,
    ExpansionPlan,
    MemoryItemKey,
    MemoryRelevance,
    PhaseResult,
    PlannerMemoryDigest,
    PlanTask,
    SkipReason,
    SubAgentInterval,
)
from personal_agent.orchestrator.fallback_planner import generate_fallback_plan
from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec
from personal_agent.orchestrator.tool_dispatch import get_shared_tool_execution_layer
from personal_agent.orchestrator.untrusted_channel import (
    MEMORY_RECALL_TOOL,
    harness_call_id,
    harness_tool_exchange,
)
from personal_agent.orchestrator.worker_types import (
    THOROUGHNESS_LEVELS,
    WORKER_TYPES,
    WorkerType,
    carries_conversation_context,
    render_worker_report_body,
    worker_types_declaring,
)
from personal_agent.request_gateway.types import TaskType

logger = structlog.get_logger(__name__)

# Plan schema: max tasks per strategy. ADR-0150 D4: no reserved extra slot — the
# combine task it held re-did the research with none of the sibling reports, and
# the primary synthesises over every result anyway.
_MAX_TASKS = {"HYBRID": 3, "DECOMPOSE": 5}
# FRE-1389: bound on how many out-of-grant gap signals one dispatch pass acts
# on — a defensive cap, not an expected count (a result's own signals are
# already capped at the source; this bounds the union across every task).
_MAX_GAP_NAMES_PER_TASK = 10

# FRE-1521: appended to the planner system prompt's Rules list. Wording set by
# master's ticket comment (Phase B, from the explore seat's Phase A V0/V1/V2
# probe): rules 1-4. Rule 5 (drop the per-level round annotation when every
# level renders the same number) is not prose for the planner to read — it is
# a formatting change to the `levels` string itself, applied below. ADR-0154
# D1 (FRE-1541): the briefing is the only path; the `current` prompt is gone.
_PLANNER_BRIEFING_RULES: tuple[str, ...] = (
    "Before writing tasks, identify the user's standing rules and the facts from "
    "the conversation that apply to this request. Put into each task's constraints "
    "every rule and fact that task depends on. The worker sees only its own task.",
    "Give every task an end point: how many items to return, and what counts as "
    "done. Never ask for all or every.",
    "Give each worker one narrow task. Do not merge separate steps (for example "
    "route timing and site checks) into one task.",
    "Do not name example places, businesses or sources in a task unless they "
    "appear in the conversation. Workers must find them.",
)

# ADR-0154 D5: the title of the memory digest block in the planner user message. The
# system prompt names the same title, so the planner knows which block to judge.
_DIGEST_TITLE = "What memory already holds, most relevant first"


def _current_sub_agent_tool_surface(trace_id: str) -> list[str]:
    """The sub-agent tool names currently grantable in the active mode (FRE-1389).

    Fails closed (empty list) on any governance/mode lookup error, matching
    ``_compute_sub_agent_grants``'s existing default-deny posture — the planner
    prompt just omits the tools rule's options.

    Args:
        trace_id: Request trace identifier, for logging.

    Returns:
        Tool names in ``config.sub_agent_tools`` eligible in the current mode
        (empty in ALERT/DEGRADED, where sub-agents hold no tools at all).
    """
    try:
        current_mode = get_current_mode()
        governance_config = load_governance_config()
    except (GovernanceConfigError, ModeManagerError) as exc:
        logger.warning("sub_agent_tool_surface_lookup_failed", error=str(exc), trace_id=trace_id)
        return []
    if current_mode in SUB_AGENT_DENIED_MODES:
        return []
    # The granted subset, never the mapping's keys (FRE-1463): a refused entry is
    # still a key, and advertising it would have the planner request a tool the
    # grant evaluation then refuses, on every turn.
    return list(governance_config.granted_sub_agent_tool_names())


def _build_planner_system_prompt(available_sub_agent_tools: list[str]) -> str:
    """Build the planner system prompt with the live sub-agent tool surface.

    Dynamic rather than hardcoded (FRE-1389 AC-1): the eligible set is read
    live from governance config so this prompt never drifts from
    ``config/governance/tools.yaml``'s ``sub_agent_tools`` list — a stale
    hardcoded name here would be the same silent-gap shape FRE-884 left
    behind (a schema field the real planner has no reason to ever populate).

    Args:
        available_sub_agent_tools: Tool names currently grantable to a
            sub-agent in the active mode (from
            :func:`_current_sub_agent_tool_surface`).

    Returns:
        The complete planner system prompt. It carries ``_PLANNER_BRIEFING_RULES``
        in the Rules list and collapses the per-level round annotation when every
        level renders the same number (FRE-1521 rule 5, a formatting change rather
        than prose).
    """
    # ADR-0150 D2: the planner picks a registry type per task, never tools. Each
    # type's description is rendered live from the registry, with the part of its
    # closed tool list governance grants right now — so the prompt still never
    # advertises a refused tool (FRE-1463), and a type stays pickable with no
    # tools (a `general` worker can answer from its own knowledge).
    surface = set(available_sub_agent_tools)
    type_lines = "\n".join(
        f"  - {worker_type.value}: {spec.description}. Tools granted now: "
        f"{', '.join(t for t in spec.tools if t in surface) or 'none'}"
        for worker_type, spec in WORKER_TYPES.items()
    )
    # ADR-0149 D2 / ADR-0150 D3: the planner is told what each level buys, rendered
    # live from the setting — a hardcoded number here drifts from the value that
    # binds, and nothing detects it (the reason FRE-1389 AC-1 gave for the tools).
    rounds_by_level = {
        level: get_settings().sub_agent_rounds_for(level) for level in THOROUGHNESS_LEVELS
    }
    if len(set(rounds_by_level.values())) == 1:
        (uniform_rounds,) = set(rounds_by_level.values())
        levels = f"{', '.join(THOROUGHNESS_LEVELS)} ({uniform_rounds} tool round(s) each)"
    else:
        levels = ", ".join(f"{level} ({n} tool round(s))" for level, n in rounds_by_level.items())
    type_names = "|".join(t.value for t in WORKER_TYPES)
    level_names = "|".join(THOROUGHNESS_LEVELS)
    prompt = (
        "You are a task decomposition planner. Given a user query and a strategy, "
        "produce a JSON plan that breaks the query into independent sub-tasks.\n\n"
        "Output ONLY valid JSON matching this schema:\n"
        '{"strategy": "HYBRID|DECOMPOSE", "tasks": [{"name": "string", '
        f'"goal": "string", "constraints": ["string"], "type": "{type_names}", '
        f'"thoroughness": "{level_names}"}}], "memory_relevance": "used|none_relevant"}}\n\n'
        "Rules:\n"
        "- Each task must be independently answerable. No task sees another task's "
        "result, and the final answer is written from all task reports together, so "
        "do not add a task that combines or synthesises other tasks' results\n"
        "- HYBRID: 1-3 tasks\n"
        "- DECOMPOSE: 2-5 tasks\n"
        "- task names must be snake_case identifiers\n"
        "- type: the kind of worker that runs the task, one of:\n"
        f"{type_lines}\n"
        f"- thoroughness: how much work the task needs, one of: {levels}. Omit it to "
        "use the type's default. A round may hold several parallel tool calls. Scope "
        "every task so a worker can answer it inside its budget. Prefer one precise "
        "task over one broad one.\n"
        # ADR-0154 D5 / ADR-0147 D3: always present, so the system prompt is byte-identical
        # with and without a digest and the cached prefix holds.
        f'- The message may hold a "{_DIGEST_TITLE}" block. A worker cannot see it, so '
        "write into each task's goal what memory already knows that the task depends on. "
        "Set memory_relevance to used when at least one memory line shaped a task goal, "
        "or to none_relevant when nothing in the block bears on the query. Without the "
        "block, omit memory_relevance\n"
        "- Do NOT answer the question — only produce the plan"
    )
    briefing_rules = "\n".join(f"- {rule}" for rule in _PLANNER_BRIEFING_RULES)
    return f"{prompt}\n{briefing_rules}"


# FRE-1548: the providers whose planner request carries the plan schema. litellm sends a
# bare ``json_object`` request to Anthropic as nothing at all, so there the JSON shape rests
# on the prompt alone. Every other provider keeps the request it was qualified on: the local
# binding qualified on ``json_object`` (FRE-1541), and the OVH deployment is left as it is.
_SCHEMA_PROVIDERS = frozenset({"anthropic"})


def planner_plan_schema(*, admit_single: bool = False) -> dict[str, Any]:
    """Build the JSON schema of the plan that the planner returns (FRE-1548).

    It is the shape ``_validate_plan_json`` checks and the system prompt asks for. The enums
    come from the registries the prompt renders from, so the three cannot drift apart.
    Every object forbids extra keys, which Anthropic's structured output requires.

    Args:
        admit_single: Whether ``SINGLE`` is a valid strategy (the decline of ADR-0154 D2).
            The production prompt does not offer it yet. The probe prompt does.

    Returns:
        A new schema dict on every call.
    """
    task = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "goal": {"type": "string"},
            "constraints": {"type": "array", "items": {"type": "string"}},
            "type": {"type": "string", "enum": [t.value for t in WORKER_TYPES]},
            "thoroughness": {"type": "string", "enum": list(THOROUGHNESS_LEVELS)},
        },
        "required": ["name", "goal", "type"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "strategy": {
                "type": "string",
                "enum": ["SINGLE", "HYBRID", "DECOMPOSE"]
                if admit_single
                else ["HYBRID", "DECOMPOSE"],
            },
            "tasks": {"type": "array", "items": task},
            "memory_relevance": {"type": "string", "enum": ["used", "none_relevant"]},
        },
        "required": ["strategy", "tasks"],
        "additionalProperties": False,
    }


def planner_response_format(provider: str | None, *, admit_single: bool = False) -> dict[str, Any]:
    """Return the ``response_format`` of the planner request for a provider (FRE-1548).

    Args:
        provider: The provider of the planner client, ``None`` for a client without one.
        admit_single: See :func:`planner_plan_schema`.

    Returns:
        A ``json_schema`` request for a provider in ``_SCHEMA_PROVIDERS``. The unchanged
        ``{"type": "json_object"}`` for every other provider.
    """
    if provider not in _SCHEMA_PROVIDERS:
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "planner_plan",
            "strict": True,
            "schema": planner_plan_schema(admit_single=admit_single),
        },
    }


def _render_planner_history(messages: list[dict[str, Any]], max_chars: int) -> str:
    """Render conversation history as plain role-labelled text (FRE-1521).

    For the planner's ``briefing`` user message — never tool-call JSON, so the
    planner reasons over prose the same way it reasons over the query. Whole
    messages are dropped from the oldest end once the budget is exceeded,
    never a mid-message truncation (which could cut a rule in half).

    Args:
        messages: Conversation messages, oldest first.
        max_chars: Character budget. A negative value is treated as zero.

    Returns:
        Newline-joined ``"role: content"`` lines, oldest kept line first. Empty
        string for no messages or an empty (or too small for even the newest
        line) budget — the budget is never exceeded, not even by one message.
    """
    max_chars = max(0, max_chars)
    lines = [f"{m.get('role', 'user')}: {get_text_content(m.get('content', ''))}" for m in messages]
    kept: list[str] = []
    line_chars = 0
    for line in reversed(lines):
        # len(kept) is the separator count "\n".join(kept) will need once this
        # line is added (one fewer than the resulting line count) — not
        # len(kept) + 1, which overcounts by one and can drop a line that
        # actually fits.
        if line_chars + len(line) + len(kept) > max_chars:
            break
        kept.append(line)
        line_chars += len(line)
    kept.reverse()
    return "\n".join(kept)


def planner_history_text(messages: list[dict[str, Any]] | None, query: str, max_chars: int) -> str:
    """Render the conversation history the planner is given for a turn (FRE-1521).

    The production caller's ``messages`` is the turn's full window and already ends
    with the current query. Left in, that last entry would render into the history
    block and the "Query:" line, so it is dropped here. The planner call and the
    ``conversation_history_chars`` record (ADR-0154 D6) both use this function, so
    the recorded size is the size the planner would receive.

    Args:
        messages: The turn's conversation window, oldest first.
        query: The turn's current user query.
        max_chars: Character budget for the render.

    Returns:
        The history render, oldest kept line first.
    """
    history_source = messages or []
    if history_source and get_text_content(history_source[-1].get("content", "")) == query:
        history_source = history_source[:-1]
    # FRE-1360 (ADR-0140 T2): this render becomes user text, so tool traffic never enters
    # it — a tool result (real, or a harness exchange carrying recalled memory or worker
    # reports) is untrusted input and reaches a model only as a tool result. An assistant
    # message that only issued tool calls has no text of its own to render.
    history_source = [
        m
        for m in history_source
        if m.get("role") != "tool"
        and not (m.get("tool_calls") and not get_text_content(m.get("content", "")).strip())
    ]
    return _render_planner_history(history_source, max_chars)


_HISTORY_HEADER = "Conversation so far:\n"
_DIGEST_HEADER = f"{_DIGEST_TITLE}:\n"
_BLOCK_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class PlannerUserMessage:
    """The planner's user message and the size of each input in it (ADR-0154 D1).

    Attributes:
        content: The planner's user message: history, then the framed query. Since
            FRE-1360 the memory digest is not in it.
        history_chars: Characters of the rendered history (0 when none fits).
        digest_chars: Characters of the digest text (0 when there is none).
        history_text: The rendered history that went into ``content`` (empty when none fits).
        message_chars: Characters of the framed query, ``Strategy: …`` to the closing
            instruction. The query is never cut.
        total_chars: ``len(content) + len(digest_block)``: every input character the
            planner receives. This is the figure the bound applies to.
        digest_block: The titled digest, carried to the planner as a ``memory_recall``
            tool result (FRE-1360, ADR-0140 T2). Empty when there is no digest.
    """

    content: str
    history_text: str
    history_chars: int
    digest_chars: int
    message_chars: int
    total_chars: int
    digest_block: str = ""


def _frame_planner_query(query: str, strategy: str) -> str:
    """Frame the query as the planner reads it: the strategy, the query, the closing instruction."""
    return f"Strategy: {strategy}\nQuery: {query}\n\nProduce the JSON plan."


def _join_planner_blocks(history_text: str, digest_text: str, tail: str) -> str:
    """Join the planner user message: history, then digest, then the framed query.

    The one place the message is assembled, so the committed probe (``scripts/eval/fre1537``)
    qualifies the text that ships. Production passes an empty digest since FRE-1360: the
    digest reaches the planner as a tool result (:func:`planner_request_messages`). The
    digest argument remains for the FRE-1537 study's design-A rendering.
    """
    parts = []
    if history_text:
        parts.append(f"{_HISTORY_HEADER}{history_text}")
    if digest_text:
        parts.append(f"{_DIGEST_HEADER}{digest_text}")
    parts.append(tail)
    return _BLOCK_SEPARATOR.join(parts)


def build_planner_user_message(
    query: str,
    strategy: str,
    messages: list[dict[str, Any]] | None,
    *,
    digest_text: str = "",
    history_max_chars: int,
    input_max_chars: int,
) -> PlannerUserMessage:
    """Build the planner user message inside ``input_max_chars`` (ADR-0154 D1).

    Fill order: the framed query is never cut. The digest keeps its own bounds. The
    history receives what remains, at most ``history_max_chars``, trimmed whole-message
    from the oldest end. The stable parts come first, so a change in the digest or the
    query never breaks the cached history before it.

    Args:
        query: The turn's current user query.
        strategy: ``"HYBRID"`` or ``"DECOMPOSE"``.
        messages: The turn's conversation window, oldest first.
        digest_text: The memory digest, already built to its own bounds. Empty for none.
        history_max_chars: ``settings.planner_history_max_chars``.
        input_max_chars: ``settings.planner_input_max_chars``, the bound on the whole message.

    Returns:
        The message and the size of each input.

    Raises:
        PlannerInputTooLargeError: The framed query and the digest alone exceed
            ``input_max_chars``.
    """
    tail = _frame_planner_query(query, strategy)
    # FRE-1360: the titled digest is a tool result of its own, so it brings no separator
    # into the user message — the bound counts its header and text, nothing more.
    fixed_chars = len(tail) + (len(_DIGEST_HEADER) + len(digest_text) if digest_text else 0)
    if fixed_chars > input_max_chars:
        raise PlannerInputTooLargeError(
            message_chars=len(tail), digest_chars=len(digest_text), max_chars=input_max_chars
        )
    history_budget = min(
        history_max_chars,
        input_max_chars - fixed_chars - len(_HISTORY_HEADER) - len(_BLOCK_SEPARATOR),
    )
    history_text = (
        planner_history_text(messages, query, history_budget) if history_budget > 0 else ""
    )
    # FRE-1360: the digest is recalled memory — untrusted input (ADR-0140 T2) — so it
    # leaves the user message and reaches the planner as a tool result
    # (planner_request_messages). The bound above still counts it.
    content = _join_planner_blocks(history_text, "", tail)
    digest_block = f"{_DIGEST_HEADER}{digest_text}" if digest_text else ""
    return PlannerUserMessage(
        content=content,
        history_text=history_text,
        history_chars=len(history_text),
        digest_chars=len(digest_text),
        message_chars=len(tail),
        total_chars=len(content) + len(digest_block),
        digest_block=digest_block,
    )


def planner_request_messages(
    system_prompt: str, planner_input: PlannerUserMessage, *, trace_id: str
) -> list[dict[str, object]]:
    """Build the planner request: system, user, and the digest as a tool result (FRE-1360).

    The digest is recalled memory. ADR-0140 T2 declares graph content untrusted, and
    untrusted input reaches a model only through a tool-result channel, so it rides a
    harness tool exchange after the user message — the last context before the planner
    generates. No digest, no exchange.

    Args:
        system_prompt: The planner system prompt.
        planner_input: From :func:`build_planner_user_message`.
        trace_id: The turn's trace id, for the exchange's call id.

    Returns:
        The message list for the planner call.
    """
    messages: list[dict[str, object]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": planner_input.content},
    ]
    if planner_input.digest_block:
        messages.extend(
            harness_tool_exchange(
                call_id=harness_call_id("mem", trace_id),
                tool_name=MEMORY_RECALL_TOOL,
                content=planner_input.digest_block,
            )
        )
    return messages


def _planner_mode_name(llm_client: Any) -> str | None:
    """Return the catalog mode that the planner client dispatches in (ADR-0154 D4).

    Args:
        llm_client: The client of the planner call.

    Returns:
        The client's default mode name, or ``None`` for a client with no catalog
        definition (a stub).
    """
    mode = getattr(getattr(llm_client, "model_def", None), "default_mode", None)
    return mode if isinstance(mode, str) else None


@dataclass(frozen=True)
class PlannerReasoning:
    """The reasoning that a planner response carried (ADR-0154 D4, D6).

    Attributes:
        chars: Reasoning characters. The largest of the client's ``reasoning_trace``, the
            provider's ``reasoning_content`` and the thinking blocks. A redacted block
            counts its payload, because its text is withheld.
        thinking_blocks: Thinking blocks in the provider message, redacted ones included.
        tokens: The reasoning tokens that the provider reported, ``None`` when it reported none.
    """

    chars: int
    thinking_blocks: int
    tokens: int | None


def _provider_message(response: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the provider message inside ``response["raw"]``, or an empty mapping."""
    raw = response.get("raw")
    choices = raw.get("choices") if isinstance(raw, Mapping) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, Mapping) else None
    return message if isinstance(message, Mapping) else {}


def _planner_reasoning(response: Mapping[str, Any]) -> PlannerReasoning:
    """Read the reasoning that a planner response carried (ADR-0154 D4, D6, FRE-1548).

    The cloud client sets ``reasoning_trace`` to ``None`` whatever the model did, so the
    evidence is also read from the provider message that the client keeps in ``raw``. The
    client's own contract does not change: the executor copies a non-empty ``reasoning_trace``
    into the next assistant message.

    Args:
        response: The normalised ``LLMResponse`` of the planner call.

    Returns:
        The reasoning characters, thinking blocks and reasoning tokens. All are zero or
        ``None`` when the response carries none.
    """
    message = _provider_message(response)
    blocks = message.get("thinking_blocks")
    block_list = [b for b in blocks if isinstance(b, Mapping)] if isinstance(blocks, list) else []
    block_chars = sum(len(str(b.get("thinking") or b.get("data") or "")) for b in block_list)
    usage = response.get("usage")
    tokens = usage.get("reasoning_tokens") if isinstance(usage, Mapping) else None
    return PlannerReasoning(
        chars=max(
            len(response.get("reasoning_trace") or ""),
            len(str(message.get("reasoning_content") or "")),
            block_chars,
        ),
        thinking_blocks=len(block_list),
        tokens=tokens if isinstance(tokens, int) and tokens >= 0 else None,
    )


def _planner_provider(llm_client: Any) -> str | None:
    """Return the provider that the planner client dispatches to (FRE-1548).

    Args:
        llm_client: The client of the planner call.

    Returns:
        The client's ``provider``, or ``None`` for a client without one (a stub).
    """
    provider = getattr(llm_client, "provider", None)
    return provider if isinstance(provider, str) else None


def _planner_deployment(llm_client: Any) -> str | None:
    """Return the deployment key that the planner client dispatches to (ADR-0154 D6).

    Args:
        llm_client: The client of the planner call.

    Returns:
        The client's ``model_key``, or ``None`` for a client without one (a stub).
    """
    key = getattr(llm_client, "model_key", None)
    return key if isinstance(key, str) else None


def _planner_usage(response: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """Read the engine's own prompt and completion token counts (ADR-0154 D6).

    Args:
        response: The normalised ``LLMResponse`` of the planner call.

    Returns:
        ``(prompt_tokens, completion_tokens)``. A count the response does not carry as a
        non-negative int is ``None``, never a guess.
    """
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return None, None
    counts = (usage.get("prompt_tokens"), usage.get("completion_tokens"))
    prompt, completion = (c if isinstance(c, int) and c >= 0 else None for c in counts)
    return prompt, completion


def resolve_memory_relevance(
    digest_items: int,
    decision: PlannerDecision,
    is_fallback: bool,
    stated: MemoryRelevance,
) -> MemoryRelevance:
    """Apply the ADR-0154 D5 matrix to the planner's stated judgment of the digest.

    ``not_applicable`` is written by the code and takes precedence over the model: on an
    empty digest the planner was never asked, and on a failed run or a fallback plan no
    planner plan exists. On a decline or an expansion with a digest, the planner's value
    stands, and ``not_applicable`` is not one of the planner's values.

    Args:
        digest_items: Lines the digest carried.
        decision: ``declined``, ``expanded`` or ``failed``.
        is_fallback: Whether the plan came from the fallback planner.
        stated: The value the plan validator recorded.

    Returns:
        The judgment the run records.
    """
    if digest_items == 0 or decision == "failed" or is_fallback:
        return "not_applicable"
    return "unstated" if stated == "not_applicable" else stated


def _memory_item_keys(keys: Sequence[MemoryItemKey]) -> list[dict[str, str | int]]:
    """Render compound memory keys as JSON-ready mappings, in order."""
    return [{"kind": k.kind, "identity": k.identity, "ordinal": k.ordinal} for k in keys]


def planner_outcome_fields(
    *,
    decision: PlannerDecision,
    failure_reason: PlannerFailureReason | None,
    is_fallback: bool,
    stated_relevance: MemoryRelevance,
    plan_task_count: int,
    digest: PlannerMemoryDigest | None,
    deployment: str | None,
    mode: str | None,
    reasoning_chars: int | None,
    duration_ms: float | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    input_chars: Mapping[str, int],
    system_prompt_sha256: str | None,
) -> dict[str, object]:
    """Build the fields of the one terminal planner event (ADR-0154 D5, ADR-0147 D5).

    Every emitter goes through this function, so no path can skip the relevance matrix
    or omit a field.

    Args:
        decision: ``declined``, ``expanded`` or ``failed``.
        failure_reason: Why the run failed; ``None`` unless ``decision == "failed"``.
        is_fallback: Whether the plan came from the fallback planner.
        stated_relevance: The value the plan validator recorded (``not_applicable`` when
            no planner plan exists).
        plan_task_count: Tasks in the plan the run returned (0 for none).
        digest: The digest the planner was given, or ``None`` for none.
        deployment: The deployment key of the planner call.
        mode: The catalog mode of the planner call.
        reasoning_chars: Reasoning characters the response carried; ``None`` when no
            response exists.
        duration_ms: Wall clock of the planner call; ``None`` when the model was not called.
        prompt_tokens: The engine's prompt-token count; ``None`` when not reported.
        completion_tokens: The engine's completion-token count; ``None`` when not reported.
        input_chars: Characters of each planner input (ADR-0154 D6).
        system_prompt_sha256: SHA-256 hex of the rendered system prompt (ADR-0154 D7);
            ``None`` when it was never rendered.

    Returns:
        The event fields, without ``trace_id``.
    """
    digest_items = digest.item_count if digest is not None else 0
    return {
        "planner_decision": decision,
        "planner_failure_reason": failure_reason,
        "is_fallback": is_fallback,
        "memory_relevance": resolve_memory_relevance(
            digest_items, decision, is_fallback, stated_relevance
        ),
        "plan_task_count": plan_task_count,
        "planner_deployment": deployment,
        "planner_mode": mode,
        "planner_reasoning_chars": reasoning_chars,
        "planner_duration_ms": duration_ms,
        "planner_prompt_tokens": prompt_tokens,
        "planner_completion_tokens": completion_tokens,
        "planner_input_chars": dict(input_chars),
        "planner_system_prompt_sha256": system_prompt_sha256,
        "memory_digest_eligible_items": digest.eligible_count if digest is not None else 0,
        "memory_digest_items": digest_items,
        "memory_digest_items_dropped": digest.dropped_count if digest is not None else 0,
        "memory_digest_kinds": dict(digest.kind_counts) if digest is not None else {},
        "memory_digest_max_line_chars": digest.max_line_chars if digest is not None else 0,
        "memory_digest_tokens": digest.estimated_tokens if digest is not None else 0,
        "memory_digest_item_keys": (
            _memory_item_keys(digest.item_keys) if digest is not None else []
        ),
        "memory_rendered_item_keys": (
            _memory_item_keys(digest.rendered_item_keys) if digest is not None else []
        ),
    }


# ADR-0154 D6: the task types whose turns record ``conversation_history_chars``.
_REGISTER_TASK_TYPES = frozenset(
    {TaskType.CONVERSATIONAL, TaskType.TOOL_USE, TaskType.ANALYSIS, TaskType.PLANNING}
)


def conversation_history_chars(
    task_type: TaskType, messages: list[dict[str, Any]] | None, max_chars: int
) -> int | None:
    """Return the length the planner's history render has, or would have, for a turn.

    A pure function of the session messages: it costs no model call and does not depend
    on whether the planner runs (ADR-0154 D6).

    Args:
        task_type: The gateway-classified task type.
        messages: The turn's conversation window, oldest first.
        max_chars: The planner's history budget (``settings.planner_history_max_chars``).

    Returns:
        The render length, or ``None`` outside the four register types.
    """
    if task_type not in _REGISTER_TASK_TYPES:
        return None
    query = get_text_content(messages[-1].get("content", "")) if messages else ""
    return len(planner_history_text(messages, query, max_chars))


def _render_worker_task(task: PlanTask) -> str:
    """Render a plan task's goal and constraints into the worker's task text (FRE-1521 AC-3).

    ``PlanTask.constraints`` reaches the worker ONLY through this text —
    ``SubAgentSpec.background`` is also set at the dispatch call site with the
    same constraints, but no prompt builder reads it
    (``_build_task_message``/``_build_sub_agent_system_prompt`` use
    ``spec.task``/``spec.worker_type``, never ``spec.background``), so a
    planner-written constraint never reached the worker before this.

    Args:
        task: The plan task to render.

    Returns:
        ``task.goal`` alone when there are no constraints, else the goal
        followed by a "Constraints:" line.
    """
    if not task.constraints:
        return task.goal
    return f"{task.goal}\nConstraints: {'; '.join(task.constraints)}"


@dataclass
class ExpansionResult:
    """Complete result of an expansion controller execution.

    Attributes:
        plan: The expansion plan (LLM-generated or fallback).
        sub_agent_results: Results from all dispatched sub-agents.
        synthesis_context: The rendered worker reports for the synthesis LLM call —
            worker-derived, so untrusted; they reach the model as a tool result
            (FRE-1360).
        synthesis_directives: The harness's own notes and closing instruction for
            the synthesis call (failed and skipped tasks, the completion count). They
            are trusted text and ride the synthesis instruction's user message
            (FRE-1360).
        phase_results: Timing and success data for each phase.
        degraded: True if graceful degradation was triggered.
        degradation_reason: Why degradation occurred, if applicable.
        planner_cost_usd: USD cost of the LLM planner call (0.0 on the fallback
            planner, which makes no LLM call) (FRE-501).
        dispatch_intervals: Wall-clock window each sub-agent occupied during
            dispatch, in plan order (FRE-1380 AC-1) — proof the fan-out ran
            sequentially, from real timestamps rather than the dispatch loop's
            own structure.
        skipped_tasks: Plan task names never dispatched because the turn's
            remaining budget (FRE-1397) was already exhausted when their turn
            came up in the serialized loop, or because an earlier worker's
            model server failed (FRE-1501). Distinct from a failed
            ``SubAgentResult`` — these never ran at all, so they are reported
            here rather than fabricated into ``sub_agent_results`` (AC-3
            mirrors why FRE-1380 deleted ``_not_admitted_result``).
        skip_reason: Why ``skipped_tasks`` were not dispatched, or ``None`` when
            nothing was skipped (FRE-1501).
        dispatched_count: Workers handed to ``run_sub_agent``, returned or raised,
            replacements included (ADR-0151 D1). Incremented on this result
            immediately before each call. Not ``len(sub_agent_results)``, which
            drops a worker that raised, and not ``len(dispatch_intervals)``, which
            also records a failed span entry and is assigned only after the loop.
    """

    plan: ExpansionPlan | None = None
    sub_agent_results: list[SubAgentResult] = field(default_factory=list)
    synthesis_context: str = ""
    synthesis_directives: str = ""
    phase_results: list[PhaseResult] = field(default_factory=list)
    degraded: bool = False
    degradation_reason: str | None = None
    planner_cost_usd: float = 0.0
    dispatch_intervals: list[SubAgentInterval] = field(default_factory=list)
    skipped_tasks: list[str] = field(default_factory=list)
    skip_reason: SkipReason | None = None
    dispatched_count: int = 0

    @property
    def cost_usd(self) -> float:
        """Total expansion cost: planner call + every dispatched sub-agent (FRE-501).

        The executor rolls this into the live turn meter ``ctx.turn_cost_usd`` so
        the PWA reflects sub-agent spend, not just the primary call.
        """
        return self.planner_cost_usd + sum(r.cost_usd for r in self.sub_agent_results)

    @property
    def successful_count(self) -> int:
        """Count of sub-agents that succeeded."""
        return sum(1 for r in self.sub_agent_results if r.success)

    @property
    def failed_count(self) -> int:
        """Count of sub-agents that failed."""
        return sum(1 for r in self.sub_agent_results if not r.success)


def _compute_sub_agent_grants(
    tasks: list[PlanTask],
    trace_id: str,
) -> list[SubAgentToolGrant]:
    """Filter each task's type tools against the sub-agent tool grant set (FRE-1388).

    A task requests exactly its worker type's closed tool list (ADR-0150 D2); the
    type is a request, and this filter is what governs it, unchanged.

    Fails safe: a governance-config or mode-lookup error denies every tool this
    dispatch requested rather than aborting the whole expansion turn. The grant
    set's own policy is already default-deny, so degrading to "deny everything"
    on a lookup failure changes no correctness guarantee.

    Args:
        tasks: Plan tasks whose type's tools to filter.
        trace_id: Request trace identifier, for logging.

    Returns:
        One :class:`SubAgentToolGrant` per task, in ``tasks`` order.
    """
    try:
        current_mode = get_current_mode()
        governance_config = load_governance_config()
    except (GovernanceConfigError, ModeManagerError) as exc:
        logger.warning(
            "sub_agent_tool_grant_lookup_failed",
            error=str(exc),
            trace_id=trace_id,
        )
        return [
            SubAgentToolGrant(
                granted=(),
                denied=WORKER_TYPES[task.type].tools,
                denial_reason=f"governance lookup failed: {exc}",
            )
            for task in tasks
        ]

    grants = [
        evaluate_sub_agent_tool_grant(
            WORKER_TYPES[task.type].tools, current_mode, governance_config
        )
        for task in tasks
    ]
    for task, grant in zip(tasks, grants, strict=True):
        if grant.denied:
            logger.warning(
                "sub_agent_tool_denied",
                task_name=task.name,
                denied_tools=list(grant.denied),
                reason=grant.denial_reason,
                mode=current_mode.value,
                trace_id=trace_id,
            )
    return grants


class ExpansionController:
    """Deterministic expansion enforcement.

    Usage:
        controller = ExpansionController()
        result = await controller.execute(
            query, strategy, llm_client, trace_id, messages,
            planner_llm_client=planner_llm_client,
        )

    Omitting planner_llm_client falls back to the dispatch (sub_agent-bound)
    client and defeats FRE-1390's fix — the planner call generates on the
    thinking-disabled deployment again, silently.
    """

    async def execute(
        self,
        query: str,
        strategy: str,
        llm_client: Any,
        trace_id: str,
        messages: list[dict[str, Any]],
        constraints: dict[str, Any] | None = None,  # TODO: wire into planner prompt
        session_id: str | None = None,
        eval_mode: bool = False,
        planner_llm_client: Any | None = None,
        turn_deadline_monotonic: float | None = None,
        user_id: UUID | None = None,
        authenticated: bool = False,
        turn_started_at: datetime | None = None,
        expansion_budget: int | None = None,
        memory_digest: PlannerMemoryDigest | None = None,
    ) -> ExpansionResult:
        """Run the full expansion pipeline.

        Args:
            query: User's original query.
            strategy: "HYBRID" or "DECOMPOSE".
            llm_client: LLM client for the dispatch (sub-agent) calls — must be
                built for role=SUB_AGENT (FRE-958).
            trace_id: Request trace identifier.
            messages: Conversation context for sub-agents.
            constraints: Optional expansion constraints from gateway.
            session_id: Originating session id for cost attribution (ADR-0074).
            eval_mode: True when the parent turn originated from an eval run; threaded
                to per-sub-agent audit records for EVAL provenance (FRE-523).
            turn_deadline_monotonic: The turn's own absolute ``time.monotonic()``
                deadline (FRE-1397) — the caller's ``turn_started_monotonic +
                min(turn_deadline_remaining, turn_lifetime_remaining)``, computed
                ONCE by the caller before this call (which itself runs the
                planner phase first). Passed through unchanged to
                ``_run_dispatch`` rather than re-derived from a duration at
                dispatch time: re-anchoring a "seconds remaining" figure to
                "now" after the planner call already ran would silently hand
                dispatch back the time the planner just spent. ``None`` (the
                default) leaves dispatch unbounded by the turn, exactly as
                today, for a caller that has not been updated to pass it.
            planner_llm_client: LLM client for the planner call — must be built
                for role=PRIMARY (FRE-1390): decomposition is a reasoning
                judgement about work that has not happened yet, and SUB_AGENT
                binds to a deployment with thinking hard-disabled. A caller's
                own client is fixed to one deployment at construction (the
                ``role`` kwarg on ``.respond()`` is a telemetry label only), so
                this must be a genuinely different client, not a request-time
                override — defaults to ``llm_client`` for a caller that has not
                been updated to build one (e.g. an existing test double).
            user_id: The turn's owning user UUID (FRE-1467). Threaded to every
                sub-agent so its granted tools are identity-scoped exactly as
                the primary's are; see ``sub_agent``'s module docstring for what
                each tool returns that it did not before.
            authenticated: Whether the turn carries a verified identity
                (FRE-229 / FRE-673). Threaded with ``user_id``; the two are read
                together by the memory visibility filter and separating them
                would half-open it.
            turn_started_at: The turn's captured timestamp (ADR-0149 D3 move 2),
                set on every dispatched spec so the worker is told today's date.
                The primary has had this since FRE-960's volatile block; the
                worker never did, and on 2026-09-10 one spent all five of its
                rounds searching the wrong year. ``None`` (the default) leaves a
                caller that has not been updated exactly as it is today, with the
                worker logging that it was given no timestamp.
            expansion_budget: The turn's own per-turn load-shed budget
                (FRE-1382, ``brainstem.compute_expansion_budget`` via
                ``GatewayOutput.governance.expansion_budget``) — tightens the
                strategy's task-count cap below, never relaxes it. ``None``
                (the default) leaves the strategy cap as the only bound, for a
                caller that has not been updated to pass it.
            memory_digest: The planner's memory digest (ADR-0154 D5), built by the caller
                from the memory context already in hand. It reaches the planner call only:
                no worker receives it (ADR-0147 D2). ``None`` gives the planner no digest.

        Returns:
            ExpansionResult with plan, sub-agent results, and synthesis context.
        """
        result = ExpansionResult()
        settings = get_settings()

        # --- Phase 1: Planning ---
        plan = await self._run_planner(
            query=query,
            strategy=strategy,
            llm_client=planner_llm_client if planner_llm_client is not None else llm_client,
            trace_id=trace_id,
            timeout_s=settings.planner_timeout_seconds,
            result=result,
            session_id=session_id,
            user_id=user_id,
            authenticated=authenticated,
            expansion_budget=expansion_budget,
            eval_mode=eval_mode,
            # FRE-1521: the same conversation window `_run_dispatch` already
            # slices for sub-agents (messages[-4:]) — here in full, for the
            # planner's briefing user message.
            messages=messages,
            history_max_chars=settings.planner_history_max_chars,
            input_max_chars=settings.planner_input_max_chars,
            memory_digest=memory_digest,
        )
        result.plan = plan

        if not plan or not plan.tasks:
            result.degraded = True
            result.degradation_reason = "No valid plan produced"
            # ADR-0088 D5: the planner-fallback case (the 87cbd720 silent degradation) is
            # now a loud, first-class signal routed through the one sanctioned call.
            if session_id is not None:
                await report_degradation(
                    trace_id=trace_id,
                    session_id=session_id,
                    where=f"expansion:{strategy.lower()}",
                    reason="No valid plan produced",
                    severity="critical",
                    expected="a tool-using sub-agent plan",
                    actual="no plan — fall back to the primary loop",
                )
            return result

        if strategy.upper() == "HYBRID":
            logger.info(
                "hybrid_expansion_start",
                sub_agent_count=len(plan.tasks),
                trace_id=trace_id,
            )

        # --- Phase 2: Dispatch ---
        sub_results = await self._run_dispatch(
            plan=plan,
            llm_client=llm_client,
            trace_id=trace_id,
            messages=messages,
            result=result,
            session_id=session_id,
            eval_mode=eval_mode,
            turn_deadline_monotonic=turn_deadline_monotonic,
            user_id=user_id,
            authenticated=authenticated,
            turn_started_at=turn_started_at,
        )
        result.sub_agent_results = sub_results

        # Check for total failure
        if sub_results and all(not r.success for r in sub_results):
            result.degraded = True
            result.degradation_reason = "All sub-agents failed"
            logger.warning(
                "graceful_degradation_triggered",
                phase="executor",
                reason="all_subagents_failed",
                trace_id=trace_id,
            )
            if session_id is not None:
                await report_degradation(
                    trace_id=trace_id,
                    session_id=session_id,
                    where=f"expansion:{strategy.lower()}",
                    reason="All sub-agents failed",
                    severity="critical",
                )
        elif not sub_results:
            result.degraded = True
            result.degradation_reason = "No sub-agent results"
            if session_id is not None:
                await report_degradation(
                    trace_id=trace_id,
                    session_id=session_id,
                    where=f"expansion:{strategy.lower()}",
                    reason="No sub-agent results",
                    severity="warning",
                )

        # --- Build synthesis context ---
        result.synthesis_context = self._build_synthesis_context(plan=plan, sub_results=sub_results)
        result.synthesis_directives = self._build_synthesis_directives(
            sub_results=sub_results,
            skipped_tasks=result.skipped_tasks,
            skip_reason=result.skip_reason,
        )

        if strategy.upper() == "HYBRID":
            logger.info(
                "hybrid_expansion_complete",
                total=len(sub_results),
                successes=result.successful_count,
                failures=result.failed_count,
                trace_id=trace_id,
            )

        return result

    async def _run_planner(
        self,
        query: str,
        strategy: str,
        llm_client: Any,
        trace_id: str,
        timeout_s: float,
        result: ExpansionResult,
        session_id: str | None = None,
        user_id: UUID | None = None,
        authenticated: bool = False,
        eval_mode: bool = False,
        expansion_budget: int | None = None,
        messages: list[dict[str, Any]] | None = None,
        history_max_chars: int = 60000,
        input_max_chars: int | None = None,
        memory_digest: PlannerMemoryDigest | None = None,
    ) -> ExpansionPlan:
        """Phase 1: Get a plan from the LLM or fallback planner.

        Args:
            query: User's original query.
            strategy: "HYBRID" or "DECOMPOSE".
            llm_client: LLM client for the planner call.
            trace_id: Request trace identifier.
            timeout_s: Planner timeout in seconds.
            result: ExpansionResult to append phase data to.
            session_id: Originating session id for cost attribution (ADR-0074).
            user_id: The turn's owning user UUID (FRE-1467). The planner calls no
                tool, so nothing about its behaviour changes; it is carried so
                that every LLM call inside expansion answers "whose turn is
                this?" the same way, rather than two ways.
            authenticated: Whether the turn carries a verified identity. Carried
                for the same reason as ``user_id``.
            eval_mode: EVAL provenance (FRE-523 / FRE-375). Carried for the same
                reason: the planner's context otherwise disagreed with the
                worker's about which turn it belongs to.
            expansion_budget: The turn's own per-turn load-shed budget
                (FRE-1382, ``brainstem.compute_expansion_budget``) — tightens
                the strategy's ``_MAX_TASKS`` cap below, on both the LLM plan
                and the fallback plan, never relaxes it. ``None`` (the
                default) leaves the strategy cap as the only bound.
            messages: The turn's conversation window (FRE-1521), the same one
                ``_run_dispatch`` slices for sub-agents. Rendered into the
                planner user message as the conversation history. ``None``
                renders no history.
            history_max_chars: ``settings.planner_history_max_chars``
                (FRE-1521) — the character budget for the rendered history.
            input_max_chars: The bound on the whole planner user message (ADR-0154 D1).
                ``None`` (the default) reads ``settings.planner_input_max_chars``, so a
                caller that omits it never carries a copy of the value. When the query
                and the digest alone exceed it, the planner is not called and
                the attempt takes the failure path with reason
                ``input_too_large``.
            memory_digest: The memory digest (ADR-0154 D5). Its text goes in the user
                message after the history and before the query. ``None`` gives no digest.

        Returns:
            An ExpansionPlan — either LLM-generated or fallback. Exactly one
            ``planner_outcome`` event records the run (ADR-0154 D5).
        """
        start_ms = time.monotonic() * 1000

        logger.info("planner_started", strategy=strategy, trace_id=trace_id)

        # Read once, inside the try (an unexpected lookup error still reaches the
        # fallback), and shared with the fallback planner so both plan against the
        # same grant surface. Empty until read, which fails closed to `general`.
        tool_surface: list[str] = []
        # ADR-0154 D6: the characters of each input, for `planner_completed` and for an
        # `input_too_large` failure. Zero until measured, never a sentinel.
        input_chars = {"system": 0, "history": 0, "digest": 0, "message": 0}
        # ADR-0154 D5: the facts of this run for its one terminal `planner_outcome` event,
        # which is emitted outside the try, so a failure in an emitter cannot add a second.
        llm_plan: ExpansionPlan | None = None
        failure_reason: PlannerFailureReason | None = None
        reasoning_chars: int | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        call_started: float | None = None
        call_duration_ms: float | None = None
        system_prompt_sha256: str | None = None
        digest_text = memory_digest.text if memory_digest is not None else ""
        try:
            tool_surface = _current_sub_agent_tool_surface(trace_id)
            planner_system_prompt = _build_planner_system_prompt(tool_surface)
            input_chars["system"] = len(planner_system_prompt)
            system_prompt_sha256 = hashlib.sha256(planner_system_prompt.encode()).hexdigest()
            # ADR-0154 D1: the query is never cut, and the history receives what the bound
            # leaves. The last message is dropped when it is the current query (see
            # planner_history_text), so it is not rendered twice.
            planner_input = build_planner_user_message(
                query,
                strategy,
                messages,
                digest_text=digest_text,
                history_max_chars=history_max_chars,
                input_max_chars=(
                    input_max_chars
                    if input_max_chars is not None
                    else get_settings().planner_input_max_chars
                ),
            )
            input_chars.update(
                history=planner_input.history_chars,
                digest=planner_input.digest_chars,
                message=planner_input.message_chars,
            )
            planner_messages = planner_request_messages(
                planner_system_prompt, planner_input, trace_id=trace_id
            )

            from personal_agent.telemetry.trace import TraceContext

            call_started = time.monotonic()
            raw_response = await asyncio.wait_for(
                llm_client.respond(
                    # FRE-1390: decomposition is a reasoning judgement about work
                    # that has not happened yet, and nothing downstream re-opens
                    # a bad plan. SUB_AGENT resolves to its own worker mode with
                    # thinking hard-disabled (ADR-0145 D1, config/model_roles.yaml);
                    # PRIMARY is the thinking-capable deployment the plan's own
                    # output will be judged against.
                    role=ModelRole.PRIMARY,
                    messages=planner_messages,
                    # FRE-1413: no max_tokens override here. The old hardcoded
                    # 1024 was sized for the retired thinking-disabled SUB_AGENT
                    # call — on llama.cpp the completion budget includes
                    # thinking (ADR-0141 D5), so it silently cut PRIMARY off
                    # mid-reasoning. Omitting the kwarg defers to the resolved
                    # client's own catalog ceiling, exactly like every other
                    # `.respond()` call in the orchestrator's main turn loop.
                    # FRE-1548: a provider that does not enforce JSON on its own gets the
                    # plan schema. Every other provider keeps the bare request it was
                    # qualified on.
                    response_format=planner_response_format(_planner_provider(llm_client)),
                    trace_ctx=TraceContext(
                        trace_id=trace_id,
                        user_id=user_id,
                        session_id=session_id,
                        eval_mode=eval_mode,
                        authenticated=authenticated,
                    ),
                ),
                timeout=timeout_s,
            )

            duration_ms = time.monotonic() * 1000 - start_ms
            call_duration_ms = (time.monotonic() - call_started) * 1000
            prompt_tokens, completion_tokens = _planner_usage(raw_response)
            # FRE-501: capture planner-call cost so the executor can roll it into
            # the live turn meter. Paid/cloud calls populate cost_usd; 0.0 otherwise.
            result.planner_cost_usd = float(raw_response.get("cost_usd") or 0.0)
            reasoning = _planner_reasoning(raw_response)
            reasoning_chars = reasoning.chars
            plan = _validate_plan_json(
                raw_response["content"], strategy, max_tasks=expansion_budget
            )

            if plan is not None:
                result.phase_results.append(
                    PhaseResult(
                        phase=ExpansionPhase.PLANNING,
                        duration_ms=duration_ms,
                        success=True,
                    )
                )
                logger.info(
                    "planner_completed",
                    plan_task_count=len(plan.tasks),
                    task_types=[task.type.value for task in plan.tasks],
                    task_thoroughness=[task.thoroughness for task in plan.tasks],
                    parse_success=True,
                    fallback_used=False,
                    # FRE-1521 AC-4: how much history the planner saw, and how many
                    # constraints per task it wrote.
                    history_chars=input_chars["history"],
                    task_constraints_count=[len(task.constraints) for task in plan.tasks],
                    # ADR-0154 D4/D6: the evidence is the response, not the request. The
                    # mode that ran and the reasoning the response carried show on the
                    # first turn a mode stops disabling thinking.
                    planner_mode=_planner_mode_name(llm_client),
                    planner_reasoning_chars=reasoning_chars,
                    # FRE-1548: what a managed provider reports (the cloud client keeps no
                    # reasoning_trace), so ADR-0154 D6 shows the truth on any deployment.
                    planner_thinking_blocks=reasoning.thinking_blocks,
                    planner_reasoning_tokens=reasoning.tokens,
                    planner_input_chars=input_chars,
                    planner_input_total_chars=planner_input.total_chars,
                    trace_id=trace_id,
                )
                llm_plan = plan
            else:
                failure_reason = "invalid"
                # FRE-1413 AC-3: a response cut off at the token ceiling
                # (finish_reason == "length") must not surface identically to a
                # genuinely malformed one — that ambiguity is what let the
                # FRE-1390 cap-sizing defect run unnoticed. Checked only once
                # validation has already failed: the prompt requires bare JSON
                # with nothing after it, so a successful parse is accepted as
                # complete regardless of finish_reason.
                truncated = raw_response.get("finish_reason") == "length"
                logger.warning(
                    "planner_failed",
                    reason="output_truncated" if truncated else "schema_validation_failed",
                    finish_reason=raw_response.get("finish_reason"),
                    trace_id=trace_id,
                )

        except asyncio.TimeoutError:
            failure_reason = "timeout"
            logger.warning(
                "planner_failed",
                reason="timeout",
                trace_id=trace_id,
            )

        except PlannerInputTooLargeError as exc:
            # ADR-0154 D1: the model was not called. Until the routing change (FRE-1515)
            # this follows today's failure path, the fallback planner.
            failure_reason = "input_too_large"
            input_chars.update(message=exc.message_chars, digest=exc.digest_chars)
            logger.warning(
                "planner_failed",
                reason="input_too_large",
                planner_input_chars=input_chars,
                input_max_chars=exc.max_chars,
                trace_id=trace_id,
            )

        except Exception as exc:
            failure_reason = "exception"
            logger.warning(
                "planner_failed",
                reason="exception",
                error=str(exc),
                trace_id=trace_id,
            )

        mode = _planner_mode_name(llm_client)
        deployment = _planner_deployment(llm_client)
        if call_duration_ms is None and call_started is not None:
            # A timeout or an exception during the call: the call still ran this long.
            call_duration_ms = (time.monotonic() - call_started) * 1000
        if llm_plan is not None:
            llm_plan = replace(
                llm_plan,
                memory_relevance=resolve_memory_relevance(
                    memory_digest.item_count if memory_digest is not None else 0,
                    "expanded",
                    False,
                    llm_plan.memory_relevance,
                ),
            )
            logger.info(
                "planner_outcome",
                **planner_outcome_fields(
                    decision="expanded",
                    failure_reason=None,
                    is_fallback=False,
                    stated_relevance=llm_plan.memory_relevance,
                    plan_task_count=len(llm_plan.tasks),
                    digest=memory_digest,
                    deployment=deployment,
                    mode=mode,
                    reasoning_chars=reasoning_chars,
                    duration_ms=call_duration_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    input_chars=input_chars,
                    system_prompt_sha256=system_prompt_sha256,
                ),
                trace_id=trace_id,
            )
            return llm_plan

        # --- Fallback planner ---
        fallback_plan = generate_fallback_plan(
            query=query,
            strategy=strategy,
            sub_agent_tool_surface=tool_surface,
            max_tasks=expansion_budget,
        )
        duration_ms = time.monotonic() * 1000 - start_ms

        result.phase_results.append(
            PhaseResult(
                phase=ExpansionPhase.PLANNING,
                duration_ms=duration_ms,
                success=True,
            )
        )

        logger.info(
            "fallback_planner_used",
            reason="planner_failure",
            task_count=len(fallback_plan.tasks),
            trace_id=trace_id,
        )
        # ADR-0154 D5: a run that fails and then uses the fallback plan is one run, and
        # its one event records the fallback.
        logger.info(
            "planner_outcome",
            **planner_outcome_fields(
                decision="failed",
                failure_reason=failure_reason or "exception",
                is_fallback=True,
                stated_relevance=fallback_plan.memory_relevance,
                plan_task_count=len(fallback_plan.tasks),
                digest=memory_digest,
                deployment=deployment,
                mode=mode,
                reasoning_chars=reasoning_chars,
                duration_ms=call_duration_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                input_chars=input_chars,
                system_prompt_sha256=system_prompt_sha256,
            ),
            trace_id=trace_id,
        )

        return fallback_plan

    async def _run_dispatch(
        self,
        plan: ExpansionPlan,
        llm_client: Any,
        trace_id: str,
        messages: list[dict[str, Any]],
        result: ExpansionResult,
        session_id: str | None = None,
        eval_mode: bool = False,
        turn_deadline_monotonic: float | None = None,
        user_id: UUID | None = None,
        authenticated: bool = False,
        turn_started_at: datetime | None = None,
    ) -> list[SubAgentResult]:
        """Phase 2: Dispatch sub-agents sequentially, one task at a time.

        FRE-1380 (owner direction, 2026-09-04): the fan-out is serialized, not
        concurrent. slm_server's own concurrency benchmark shows aggregate
        throughput rising only ~19% from concurrency 1→3 while per-request
        throughput falls 2.57x (docs/reference/SLM_SERVER_CLIENT_SEMANTICS.md), so
        concurrent dispatch never bought the wall-clock win it appeared to.
        Sub-agents exist for context isolation — a digest reaches synthesis, never
        the full transcript — and that property holds identically whether tasks
        run side by side or one after another. Serializing also deletes the
        FRE-1374 admission race outright: with no concurrency ceiling to queue
        behind, no task can ever fail to be admitted, so the fan-out's former
        per-window admission timeout setting and its "not admitted" result no
        longer exist (AC-3) — the failure mode is gone, not merely rarer.

        Args:
            plan: Validated expansion plan with tasks.
            llm_client: LLM client for sub-agent inference calls.
            trace_id: Request trace identifier.
            messages: Conversation context window slice for sub-agents.
            result: ExpansionResult to append phase data to.
            session_id: Originating session id for cost attribution (ADR-0074).
            eval_mode: True when the parent turn originated from an eval run; threaded
                to per-sub-agent audit records for EVAL provenance (FRE-523).
            turn_deadline_monotonic: The turn's absolute ``time.monotonic()``
                deadline (FRE-1397), or ``None`` for no bound (today's
                behavior). Re-checked fresh before each serialized task: once
                the remaining time hits zero, every task still queued is
                skipped outright — recorded in ``result.skipped_tasks``, never
                given a fabricated ``SubAgentResult`` (AC-3) — and each
                dispatched task's own deadline is capped to whatever remains
                at that moment, never divided up-front across the plan. Since
                a task's actual run time can never exceed the deadline it was
                given, and that deadline can never exceed what was left when
                it started, the cumulative dispatch time can never exceed
                ``turn_deadline_monotonic`` by more than one task's own small
                post-``wait_for`` cleanup overhead (already documented as
                negligible against these 60-300s budgets elsewhere on
                ``SubAgentResult.elapsed_generation_ms``).
            user_id: The turn's owning user UUID (FRE-1467), threaded to every
                worker call this method makes — the per-task dispatch AND the
                replacement dispatch below. Both, or a granted tool works on the
                first attempt and fails on the retry.
            authenticated: Whether the turn carries a verified identity, threaded
                on the same two paths as ``user_id``.
            turn_started_at: The turn's captured timestamp (ADR-0149 D3 move 2),
                set on every spec this method builds — the per-task specs below
                and, through ``dataclasses.replace``, the replacement spec in
                ``_maybe_redispatch_on_gap``. Both, or a retried worker loses the
                date the first attempt had.

        Returns:
            List of SubAgentResult, in dispatch order — one entry per task that
            returned a result (success or a reported per-task failure), plus one
            extra entry immediately after any task whose result triggered a
            single-shot replacement dispatch on a stated tool gap (FRE-1389
            AC-5) — both the original and the replacement are kept, so
            ``ExpansionResult.cost_usd`` never silently drops the first,
            incomplete attempt's cost. A task whose dispatch raised a raw
            exception, or that was skipped for turn-budget exhaustion, is
            dropped from this list; see ``result.dispatch_intervals`` for the
            complete per-task record of what raised, and
            ``result.skipped_tasks`` for what never ran.
        """
        start_ms = time.monotonic() * 1000

        logger.info(
            "expansion_dispatch_started",
            task_count=len(plan.tasks),
            trace_id=trace_id,
        )

        # FRE-1388: a sub-agent is a distinct governance principal from the
        # primary. The task's type tools (ADR-0150 D2, the type the planner
        # picked) are filtered against the sub-agent tool grant set here — never
        # passed through unfiltered, and never checked against the primary's own
        # per-tool `allowed_in_modes`.
        grants = _compute_sub_agent_grants(plan.tasks, trace_id)

        specs = [
            SubAgentSpec(
                # FRE-1521 AC-3: constraints rendered into the task text itself —
                # see _render_worker_task's docstring for why `background` below
                # cannot carry them to the worker.
                task=_render_worker_task(task),
                # FRE-1564: a type that can send text out is briefed by the task text alone.
                context=(
                    messages[-4:]
                    if messages and carries_conversation_context(WORKER_TYPES[task.type])
                    else []
                ),
                # FRE-1379: no max_tokens override here — SubAgentSpec's own
                # default (None) defers to the deployment's catalog-declared
                # ceiling. settings.sub_agent_max_tokens used to be passed here
                # unconditionally and silently shadowed the catalog's own
                # (smaller, deliberately sized) value on every call; that
                # setting's only other reader was the autonomous-mode
                # decomposition path, deleted in FRE-1381 along with the field.
                # ADR-0145 D1 (FRE-1444): neither budget is pinned here any more. Both
                # settings reads shadowed the `sub_agent` role's own `default_timeout`
                # — an explicit `timeout_s` beats the deployment's declaration inside
                # the client, so the role's budget could never bind. Omitting them
                # leaves one owner for the number: the role.
                tools=list(grant.granted),
                background=(f"Sub-task: {task.name}. Constraints: {', '.join(task.constraints)}"),
                mode=task.mode,
                denied_tools=grant.denied,
                turn_started_at=turn_started_at,
                worker_type=task.type,
                thoroughness=task.thoroughness,
                # ADR-0150 D5: isolation by partition — each worker is told what
                # its siblings own, so it stays inside its own task.
                sibling_tasks=tuple(other.name for other in plan.tasks if other is not task),
            )
            for task, grant in zip(plan.tasks, grants, strict=True)
        ]

        dispatch_start = time.monotonic()

        # ADR-0123 §1/AC-8 (FRE-934): a sub-agent fan-out is one parent EXPANSION
        # phase with N sequential SUB_AGENT children, each with its own lifecycle.
        # Each child span wraps one run_sub_agent call so its end fires as that
        # agent finishes (or raises); the parent span's end fires only after the
        # last child's dispatch completes.
        from personal_agent.transport.agui.transport import phase_span  # noqa: PLC0415
        from personal_agent.transport.events import Phase  # noqa: PLC0415

        raw_results: list[SubAgentResult | Exception] = []
        intervals: list[SubAgentInterval] = []
        sub_results: list[SubAgentResult] = []

        # FRE-1501: set once any worker's model server answers 5xx after the
        # client's retries. On 2026-09-12 one worker's 500 was followed within
        # 20 seconds by 503 for every caller, and the next sibling started into it.
        # No timed wait: the health probe cannot see a dead backend (FRE-1474).
        origin_failed = False

        async with phase_span(
            session_id=session_id,
            phase=Phase.EXPANSION,
            detail=f"{len(specs)} sub-agents",
        ) as _parent_id:
            for task, spec in zip(plan.tasks, specs, strict=True):
                if origin_failed:
                    logger.warning(
                        "sub_agent_dispatch_skipped_origin_error",
                        task_name=task.name,
                        trace_id=trace_id,
                    )
                    result.skipped_tasks.append(task.name)
                    result.skip_reason = "origin_error"
                    continue

                # FRE-1397: recomputed fresh for every task rather than divided
                # up-front across the plan — most sub-agents finish well under
                # their own ceiling, so a live "whatever's left" check wastes
                # none of that headroom on tasks earlier in the loop.
                worker_max_deadline: float | None = None
                if turn_deadline_monotonic is not None:
                    remaining = turn_deadline_monotonic - time.monotonic()
                    if remaining <= 0:
                        logger.warning(
                            "sub_agent_dispatch_skipped_turn_budget_exhausted",
                            task_name=task.name,
                            trace_id=trace_id,
                        )
                        result.skipped_tasks.append(task.name)
                        result.skip_reason = "turn_budget"
                        continue
                    worker_max_deadline = remaining

                interval_start = time.monotonic()
                sub_result: SubAgentResult | None = None
                try:
                    async with phase_span(
                        session_id=session_id,
                        phase=Phase.SUB_AGENT,
                        detail=spec.task[:80],
                        parent_id=_parent_id,
                    ):
                        result.dispatched_count += 1
                        sub_result = await run_sub_agent(
                            spec=spec,
                            llm_client=llm_client,
                            trace_id=trace_id,
                            session_id=session_id,
                            eval_mode=eval_mode,
                            max_deadline_seconds=worker_max_deadline,
                            user_id=user_id,
                            authenticated=authenticated,
                        )
                except Exception as exc:
                    raw_results.append(exc)
                else:
                    raw_results.append(sub_result)
                finally:
                    intervals.append(SubAgentInterval(task.name, interval_start, time.monotonic()))

                if sub_result is None:
                    continue
                sub_results.append(sub_result)

                if sub_result.stop_reason == "origin_error":
                    # Checked before the gap replacement below, which would
                    # otherwise send a replacement worker into the same origin.
                    origin_failed = True
                    continue

                # FRE-1389 AC-5: single-shot replacement dispatch when this
                # result stated a tool gap — the controller (not the
                # sub-agent) decides whether to grant more, acting in-loop so
                # the replacement is always paired with the right task even
                # if an earlier task in this same plan raised a raw exception.
                replacement = await self._maybe_redispatch_on_gap(
                    task=task,
                    spec=spec,
                    original_result=sub_result,
                    llm_client=llm_client,
                    trace_id=trace_id,
                    session_id=session_id,
                    eval_mode=eval_mode,
                    parent_span_id=_parent_id,
                    intervals=intervals,
                    result=result,
                    turn_deadline_monotonic=turn_deadline_monotonic,
                    user_id=user_id,
                    authenticated=authenticated,
                )
                if replacement is not None:
                    sub_results.append(replacement)
                    origin_failed = replacement.stop_reason == "origin_error"

        result.dispatch_intervals = intervals
        logger.info(
            "expansion_dispatch_intervals",
            trace_id=trace_id,
            intervals=[
                {
                    "task": iv.task_name,
                    "start_s": round(iv.start_monotonic - dispatch_start, 3),
                    "end_s": round(iv.end_monotonic - dispatch_start, 3),
                }
                for iv in intervals
            ],
        )

        failed_count = len(raw_results) - len(
            [r for r in raw_results if isinstance(r, SubAgentResult)]
        )
        if failed_count > 0:
            logger.warning(
                "expansion_dispatch_partial_failure",
                total=len(raw_results),
                failed=failed_count,
                trace_id=trace_id,
            )

        duration_ms = time.monotonic() * 1000 - start_ms

        result.phase_results.append(
            PhaseResult(
                phase=ExpansionPhase.DISPATCH,
                duration_ms=duration_ms,
                success=len(sub_results) > 0,
                error=None if sub_results else "No sub-agent results",
            )
        )

        for sr in sub_results:
            logger.info(
                "subagent_completed",
                task_name=sr.spec_task,
                status="success" if sr.success else "failed",
                trace_id=trace_id,
            )

        return sub_results

    async def _maybe_redispatch_on_gap(
        self,
        task: PlanTask,
        spec: SubAgentSpec,
        original_result: SubAgentResult,
        llm_client: Any,
        trace_id: str,
        session_id: str | None,
        eval_mode: bool,
        parent_span_id: Any,
        intervals: list[SubAgentInterval],
        result: ExpansionResult,
        turn_deadline_monotonic: float | None = None,
        user_id: UUID | None = None,
        authenticated: bool = False,
    ) -> SubAgentResult | None:
        """Single-shot replacement dispatch when a sub-agent stated a tool gap (FRE-1389 AC-5).

        The sub-agent only ever REPORTS a gap — an out-of-grant tool-call
        attempt refused at the source (``refused_tool_attempts``), or its own
        ``TOOL_GAP: <name>`` sentinel (``stated_tool_gap``) — it never
        acquires the tool itself. This method, acting on the controller's
        behalf (the "primary" in the ticket's architecture section), is the
        only thing that may dispatch a replacement, and it does so at most once
        per task: the replacement's own gap signals, if any, are never checked,
        so a task cannot chain retries.

        Closed by type (ADR-0150 D2): the replacement is a worker of the one
        other registry type whose tool list declares the gap-named tool, with the
        same task, thoroughness and sibling list. Every worker that runs is a
        registry type; none runs with a widened list.

        Args:
            task: The plan task that produced ``original_result``.
            spec: The original dispatch's spec — reused via ``dataclasses.replace``
                so context/output_format/timeouts/mode carry over unchanged.
            original_result: The completed sub-agent result to check for a gap.
            llm_client: LLM client for the replacement dispatch call.
            trace_id: Request trace identifier.
            session_id: Originating session id.
            eval_mode: EVAL provenance, threaded through like the original dispatch.
            parent_span_id: The dispatch's EXPANSION phase span id, so the
                replacement's own SUB_AGENT span nests under the same parent.
            intervals: Mutable interval list; the replacement's own wall-clock
                window is appended here for dispatch-observability parity.
            result: The dispatch's ExpansionResult; its ``dispatched_count`` counts
                the replacement when it is handed to ``run_sub_agent`` (ADR-0151 D1).
            turn_deadline_monotonic: The turn's absolute deadline (FRE-1397),
                threaded from ``_run_dispatch`` — this is still one more
                serialized ``run_sub_agent`` call inside the same dispatch
                phase, so it must respect the same bound or the aggregate
                guarantee would have a hole exactly here.
            user_id: The turn's owning user UUID (FRE-1467), threaded exactly as
                for the original dispatch. A replacement is dispatched precisely
                because the original reported a missing tool, so this is the one
                call where a dropped identity is most likely to be noticed as
                "the tool still does not work".
            authenticated: Whether the turn carries a verified identity, threaded
                with ``user_id``.

        Returns:
            The replacement SubAgentResult, or ``None`` when there was no gap,
            the gap named nothing actually registered, no other type (or more
            than one) declares it, governance does not grant it to that type
            (the original refusal already stands via ``denied_tools``/the
            synthesis context), or the turn's budget is already exhausted.
        """
        from personal_agent.transport.agui.transport import phase_span  # noqa: PLC0415
        from personal_agent.transport.events import Phase  # noqa: PLC0415

        gap_names = list(
            dict.fromkeys(
                [
                    *original_result.refused_tool_attempts,
                    *([original_result.stated_tool_gap] if original_result.stated_tool_gap else []),
                ]
            )
        )[:_MAX_GAP_NAMES_PER_TASK]
        if not gap_names:
            return None

        redispatch_max_deadline: float | None = None
        if turn_deadline_monotonic is not None:
            remaining = turn_deadline_monotonic - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "sub_agent_redispatch_skipped_turn_budget_exhausted",
                    task_name=task.name,
                    trace_id=trace_id,
                )
                return None
            redispatch_max_deadline = remaining

        # Untrusted model output: don't spend a whole extra dispatch on a name
        # that isn't even a registered tool — governance would deny it anyway,
        # but this skips the round-trip.
        registry = get_shared_tool_execution_layer().registry
        gap_names = [name for name in gap_names if registry.get_tool(name) is not None]
        if not gap_names:
            return None

        # ADR-0150 D2: closed by type. A same-type worker with a widened list would
        # breach the registry, so the replacement is the ONE other type whose
        # closed list declares a gap-named tool. None, or more than one, and no
        # replacement runs — the gap reaches the primary on the original result.
        target_types = {
            declaring
            for name in gap_names
            for declaring in worker_types_declaring(name)
            if declaring != spec.worker_type
        }
        if len(target_types) != 1:
            return None
        (target_type,) = target_types

        # Reuses _compute_sub_agent_grants's existing fail-closed lookup
        # (GovernanceConfigError/ModeManagerError → deny everything) rather
        # than a second hand-rolled try/except around the same lookup.
        (new_grant,) = _compute_sub_agent_grants([replace(task, type=target_type)], trace_id)
        # The replacement must actually hold the tool the gap named; a target
        # type whose gap tool governance still refuses would re-run the same
        # task without it.
        if not set(gap_names) & set(new_grant.granted):
            return None

        logger.info(
            "sub_agent_redispatched_with_expanded_grant",
            task_name=task.name,
            stated_gap=gap_names,
            from_type=spec.worker_type.value,
            to_type=target_type.value,
            expanded_grant=list(new_grant.granted),
            trace_id=trace_id,
        )

        # Same task, thoroughness, siblings, context and date — only the type and
        # its grant change (dataclasses.replace carries the rest over).
        replacement_spec = replace(
            spec,
            # FRE-1521 AC-3: the retry note goes on the goal, before
            # _render_worker_task appends "Constraints: ..." — appending it
            # after the rendered text would read as part of the constraints.
            task=_render_worker_task(
                replace(task, goal=f"{task.goal} (retry: {target_type.value} worker)")
            ),
            worker_type=target_type,
            tools=list(new_grant.granted),
            denied_tools=new_grant.denied,
        )

        interval_start = time.monotonic()
        try:
            async with phase_span(
                session_id=session_id,
                phase=Phase.SUB_AGENT,
                detail=replacement_spec.task[:80],
                parent_id=parent_span_id,
            ):
                result.dispatched_count += 1
                replacement_result = await run_sub_agent(
                    spec=replacement_spec,
                    llm_client=llm_client,
                    trace_id=trace_id,
                    session_id=session_id,
                    eval_mode=eval_mode,
                    max_deadline_seconds=redispatch_max_deadline,
                    user_id=user_id,
                    authenticated=authenticated,
                )
            return replacement_result
        except Exception as exc:
            logger.warning(
                "sub_agent_redispatch_failed",
                task_name=task.name,
                error=str(exc),
                trace_id=trace_id,
            )
            return None
        finally:
            intervals.append(
                SubAgentInterval(f"{task.name} (retry)", interval_start, time.monotonic())
            )

    def _build_synthesis_context(
        self,
        plan: ExpansionPlan,
        sub_results: list[SubAgentResult],
    ) -> str:
        """Build the rendered worker reports from sub-agent results.

        Worker-derived text only — report bodies, gaps, error strings — so it is
        untrusted input and reaches the model as a tool result (FRE-1360, ADR-0140 T2).
        The harness's own notes and instruction are :meth:`_build_synthesis_directives`.

        Args:
            plan: The expansion plan used for this run.
            sub_results: Results from all dispatched sub-agents.

        Returns:
            The rendered reports for the parent agent.
        """
        parts = [f"## Expansion Results (strategy: {plan.strategy})\n\n"]

        # ADR-0150 D4: every worker's gaps are combined into one `Not found`
        # section at the end of the turn's context, each attributed to its own
        # task, rather than repeated per worker.
        all_gaps: list[tuple[str, str, str]] = []
        for r in sub_results:
            status = "OK" if r.success else f"FAILED: {r.error}"
            if r.report is not None:
                # A schema-backed `synthesized` landing: render findings and
                # notes from the validated report, not `r.summary` (which
                # includes the gaps this loop pulls out separately).
                body = render_worker_report_body(r.report)
                all_gaps.extend((r.spec_task, g.looked_for, g.where) for g in r.report.gaps)
            else:
                body = r.summary
            # ADR-0149 D4 (FRE-1484): the header carries the terminal facts, not
            # just OK/FAILED — a worker that completed with an empty report and a
            # worker that never got the chance to write one must not read alike.
            parts.append(
                f"### {r.spec_task} [{status}: stop={r.stop_reason}, "
                f"report={r.report_kind}, {r.tool_iterations} round(s), "
                f"{r.tool_result_chars_absorbed:,} chars absorbed]\n{body}\n\n"
            )
            if r.denied_tools:
                # FRE-1388 AC-4: denial is deterministic and lands in the report
                # itself, not only a log line — the recovery path is the primary
                # re-planning with a different grant, so it must see this here.
                parts.append(
                    f"*Tool access denied:* {', '.join(r.denied_tools)} was requested "
                    "but not granted to sub-agents; this sub-task ran without it.\n\n"
                )

        if all_gaps:
            parts.append("### Not found\n")
            parts.extend(
                f"- [{task_name}] {looked_for} (looked: {where})\n"
                for task_name, looked_for, where in all_gaps
            )
            parts.append("\n")

        return "".join(parts)

    def _build_synthesis_directives(
        self,
        sub_results: list[SubAgentResult],
        skipped_tasks: list[str] | None = None,
        skip_reason: SkipReason | None = None,
    ) -> str:
        """Build the harness's notes and closing instruction for the synthesis call.

        FRE-1360 (ADR-0140 T2): the worker reports are untrusted and reach the model as
        a tool result, where it reads instructions with scepticism. These notes are the
        harness's own instructions, so they stay trusted text in the synthesis
        instruction's user message rather than riding inside the reports.

        Args:
            sub_results: Results from all dispatched sub-agents.
            skipped_tasks: Plan task names never dispatched because the turn's
                budget ran out first (FRE-1397) or the model server failed
                (FRE-1501) — distinct from a failure: these produced no result
                at all, so they get their own note rather than being silently
                absent.
            skip_reason: Why ``skipped_tasks`` were not dispatched. ``None``
                reads as the turn budget, the only reason before FRE-1501.

        Returns:
            The notes and closing instruction. Empty when there is nothing to say.
        """
        parts: list[str] = []
        n_ok = sum(1 for r in sub_results if r.success)
        n_ledger = sum(1 for r in sub_results if not r.success and r.report_kind == "ledger")

        if any(not r.success for r in sub_results):
            failed = [r.spec_task for r in sub_results if not r.success]
            parts.append(
                f"\n**Note:** The following sub-tasks failed: {', '.join(failed)}. "
                "Synthesize from available results and note any gaps.\n"
            )

        if skipped_tasks:
            parts.append(
                f"\n**Note:** The following sub-tasks were not run — "
                f"{SKIP_REASON_TEXT[skip_reason or 'turn_budget']}: {', '.join(skipped_tasks)}. "
                "Synthesize from available results and note this gap.\n"
            )

        if sub_results:
            # ADR-0149 D4 (FRE-1484): replaces the old "the sub-tasks above have
            # been completed" close, which followed the failure note even when
            # every worker failed. The last sentence is an instruction and not
            # the enforcement (that is the D4 pause) — kept because it is true.
            n_partial = len(sub_results) - n_ok - n_ledger
            parts.append(
                f"\n{n_ok} of {len(sub_results)} sub-tasks completed. {n_partial} "
                "stopped at their budget and wrote a partial report. "
                f"{n_ledger} stopped without a report; their ledger lists what "
                "they searched. Synthesize from these results only. Where a "
                "sub-task did not complete, say so in your answer rather than "
                "filling the gap from memory.\n"
            )

        return "".join(parts)


def _validate_plan_json(
    raw: str,
    strategy: str = "HYBRID",
    max_tasks: int | None = None,
) -> ExpansionPlan | None:
    """Validate LLM output against the plan schema.

    Args:
        raw: Raw string from the LLM planner response.
        strategy: Expected strategy — used as fallback if not in JSON.
        max_tasks: The turn's own per-turn expansion budget (FRE-1382) —
            tightens the strategy cap below, never relaxes it. A negative
            value is treated as zero, which fails validation the same way an
            empty ``tasks`` list already does. ``None`` (the default) leaves
            the strategy cap as the only bound.

    Returns:
        A validated ExpansionPlan, or None if the input fails validation.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    tasks_raw = data.get("tasks")
    if not isinstance(tasks_raw, list) or len(tasks_raw) == 0:
        return None

    strategy_cap = _MAX_TASKS.get(strategy, _MAX_TASKS["HYBRID"])
    # max(0, ...): a negative budget must never widen the cap via Python's
    # negative-index slicing (tasks_raw[:-1] below would otherwise keep all
    # but the last task instead of none).
    max_tasks = strategy_cap if max_tasks is None else max(0, min(strategy_cap, max_tasks))
    tasks: list[PlanTask] = []

    # ADR-0150 D4: no reserved slot. A legacy `tools` / `expected_output` key is
    # ignored — the type decides both now.
    for t in tasks_raw[:max_tasks]:
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        goal = t.get("goal")
        if not name or not goal:
            return None

        # ADR-0150 D2: the registry is closed. A missing or unknown type makes
        # the whole plan invalid, which reaches the fallback planner — an unknown
        # type never reaches dispatch.
        try:
            worker_type = WorkerType(t.get("type"))
        except ValueError:
            return None
        raw_level = t.get("thoroughness")
        if raw_level is None:
            thoroughness = WORKER_TYPES[worker_type].default_thoroughness
        else:
            level = next((lvl for lvl in THOROUGHNESS_LEVELS if lvl == raw_level), None)
            if level is None:
                return None
            thoroughness = level

        tasks.append(
            PlanTask(
                name=str(name),
                goal=str(goal),
                type=worker_type,
                thoroughness=thoroughness,
                constraints=[str(c) for c in t.get("constraints", [])],
            )
        )

    if not tasks:
        return None

    # ADR-0147 D3: the planner writes `used` or `none_relevant`. A missing or invalid
    # value is `unstated`; `_run_planner` then applies the ADR-0154 D5 matrix.
    stated = data.get("memory_relevance")
    memory_relevance: MemoryRelevance = "unstated"
    if stated == "used":
        memory_relevance = "used"
    elif stated == "none_relevant":
        memory_relevance = "none_relevant"

    return ExpansionPlan(
        strategy=data.get("strategy", strategy),
        tasks=tasks,
        is_fallback=False,
        memory_relevance=memory_relevance,
    )
