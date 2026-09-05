"""Sub-agent contract types for Cognitive Architecture Redesign v2.

Sub-agents are task-scoped inference calls — NOT separate services or processes.
Each represents one focused LLM call with a well-defined input/output contract.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.6
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from personal_agent.llm_client import ModelRole
from personal_agent.orchestrator.expansion_types import SubAgentMode


@dataclass(frozen=True)
class SubAgentSpec:
    """Specification for a single sub-agent inference call.

    Defines everything the sub-agent executor needs to run one focused
    LLM call. All fields are immutable once created.

    Attributes:
        task: Human-readable description of the sub-task to perform.
        context: Subset of context relevant to this sub-task
            (messages, retrieved docs, tool results, etc.).
        output_format: Expected output shape — e.g. "text", "json",
            "bullet_list", "code". Used by synthesiser to interpret results.
        max_tokens: Token ceiling for this sub-agent's response. ``None`` (the
            default) defers to the deployment's own catalog-declared ceiling
            (FRE-1379) — a caller passing an explicit value is a deliberate
            override of that declaration, not a competitor to it. Before
            FRE-1379 this defaulted to a hardcoded 4096 that the dispatch call
            site (``expansion_controller.py``) always supplied, silently
            shadowing the catalog's own (smaller, deliberately sized) value on
            every call — the same "knob that reads load-bearing and is not"
            shape as ``timeout_seconds`` below.
        timeout_seconds: Generation timeout for this sub-agent call, passed to the LLM
            client as ``timeout_s`` — measured from concurrency-slot acquisition, not
            from spawn (FRE-1374).
        hard_deadline_seconds: Spawn-to-completion safety net — a separate, larger
            budget bounding the whole call (including any slot wait) in case the
            underlying client ignores ``timeout_seconds``. ``None`` falls back to
            ``timeout_seconds`` (today's behavior, for callers that don't set it).
        tools: Tool names the sub-agent is allowed to invoke (empty = none). Already
            filtered against the sub-agent tool principal's grant set (FRE-1388) —
            a caller populates this with the *granted* subset, never the raw request.
        background: Background context injected into the sub-agent's system
            prompt (parent task summary, constraints, etc.).
        model_role: Model role to use for inference. Defaults to SUB_AGENT (ADR-0033).
        mode: Execution mode (ADR-0036); currently always PARALLEL_INFERENCE.
        denied_tools: Tool names this task requested that the sub-agent tool grant
            set refused (FRE-1388) — informational, carried through to
            :class:`SubAgentResult` so the refusal is legible to the primary
            (AC-4) rather than only a log line. Note this grant is necessary but
            not sufficient: a name in ``tools`` still passes through
            ``dispatch_tool_call`` -> ``ToolExecutionLayer.execute_tool``, which
            enforces that primitive's own ``allowed_modes``/``forbidden_in_modes``
            on top — the sub-agent principal can never grant access the tool's own
            base policy forbids.
    """

    task: str
    context: list[dict[str, Any]]
    output_format: str = "text"
    max_tokens: int | None = None
    timeout_seconds: float = 120.0
    hard_deadline_seconds: float | None = None
    tools: list[str] = field(default_factory=list)
    background: str = ""
    model_role: ModelRole = ModelRole.SUB_AGENT
    mode: SubAgentMode = SubAgentMode.PARALLEL_INFERENCE
    # Phase B skill routing: compact index injected into sub-agent system prompt
    skill_index_block: str = ""
    # Skills already loaded by parent — sub-agent inherits to avoid re-emitting bodies
    loaded_skills: frozenset[str] = field(default_factory=frozenset)
    denied_tools: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SubAgentResult:
    """Result of a single sub-agent inference call.

    Separates compact summary (used in synthesis context) from full output
    (stored in Elasticsearch for observability).

    Attributes:
        task_id: Unique identifier for this sub-agent invocation. A ``UUID`` so it can
            key the ``(trace_id, task_id)`` route-trace segment row (ADR-0088, FRE-517);
            stringified at wire/log/ES boundaries.
        spec_task: Original task string from the SubAgentSpec.
        summary: The response text injected into the parent agent's synthesis
            context, capped at ``sub_agent._SUMMARY_CAP_CHARS`` (25,000 chars,
            FRE-1387) — a circuit breaker sized above every generation ceiling
            in the system, not a routine summarisation budget. Equal to
            ``full_output`` unless that cap actually fired.
        full_output: Complete sub-agent response, stored to ES for observability.
        tools_used: Names of tools actually invoked during this call.
        token_count: Total tokens consumed (prompt + completion). Word-count
            approximation, not a real tokenizer count. 0 on any failure path.
        duration_ms: Wall-clock execution time in milliseconds.
        success: True if the call completed without error.
        error: Error message if success=False, None otherwise.
        cost_usd: Summed USD cost of every LLM call this sub-agent made
            (paid/cloud calls only; 0.0 for free local calls or when a
            timeout/exception loses the partial figure). Rolled into the live
            turn meter ``ctx.turn_cost_usd`` by the executor (FRE-501).
        tokens_generated: Word-count estimate of the completion actually
            generated — same approximation convention as ``token_count``, but
            populated on every terminal path including a killed one (FRE-1379),
            so a fan-out's survivors and its casualties are comparable on the
            same field for the first time. 0 when nothing was generated
            (failure before any streamed content, or a non-streaming/cloud call
            with no progress sink).
        elapsed_generation_ms: Time from the first streamed chunk received to
            when this result was built — the *generation* budget's own clock,
            distinct from ``duration_ms`` (wall time since spawn, which also
            includes any concurrency-slot wait and connect/prompt-processing
            time before the first token). ``None`` when no streaming progress
            was recorded (cloud placement, or failure before any chunk
            arrived). May include a few milliseconds of post-cancellation
            cleanup on a killed sub-agent — negligible against the 60-85s
            budgets this exists to characterise.
        denied_tools: Tool names this task requested that were refused by the
            sub-agent tool grant set (FRE-1388), copied from
            ``SubAgentSpec.denied_tools`` on every terminal path (success,
            timeout, cancellation, exception). Empty when nothing was denied.
        tool_iterations: Number of tool-execution rounds actually run (FRE-1389
            AC-2), bounded by ``settings.sub_agent_max_tool_iterations`` — the
            sub-agent's own cap, not the primary's per-TaskType one.
        tool_result_chars_absorbed: Sum of every tool-role message's raw content
            length the sub-agent fed back into its own next inference call —
            successful dispatches, failed dispatches, and synthetic refusal/
            malformed-argument errors alike, since all of it is context the
            sub-agent absorbed and none of it crosses into ``summary`` (FRE-1389
            AC-4). Compare against ``len(summary)`` to measure the isolation
            this mechanism exists to provide.
        refused_tool_attempts: Tool names the model tried to call mid-loop that
            were NOT in ``SubAgentSpec.tools`` — refused without dispatch
            (FRE-1389 AC-3's seeded negative). Distinct from ``denied_tools``,
            which is the governance-level refusal of the planner's *request*
            before this spec even existed.
        stated_tool_gap: A tool name the sub-agent explicitly reported needing
            but did not have, via the ``TOOL_GAP: <name>`` sentinel line in its
            final response (stripped from ``summary``/``full_output``). ``None``
            when no gap was stated. Together with ``refused_tool_attempts``,
            this is the signal the expansion controller uses to decide whether
            to dispatch a replacement with an expanded grant (FRE-1389 AC-5) —
            the sub-agent only ever reports the gap; it never acquires the tool
            itself.
    """

    task_id: UUID
    spec_task: str
    summary: str
    full_output: str
    tools_used: list[str]
    token_count: int
    duration_ms: float
    success: bool
    error: str | None = None
    cost_usd: float = 0.0
    tokens_generated: int = 0
    elapsed_generation_ms: float | None = None
    denied_tools: tuple[str, ...] = field(default_factory=tuple)
    tool_iterations: int = 0
    tool_result_chars_absorbed: int = 0
    refused_tool_attempts: tuple[str, ...] = field(default_factory=tuple)
    stated_tool_gap: str | None = None
