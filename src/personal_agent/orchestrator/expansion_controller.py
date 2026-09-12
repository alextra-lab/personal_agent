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
import json
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from personal_agent.brainstem import ModeManagerError, get_current_mode
from personal_agent.config import GovernanceConfigError, get_settings, load_governance_config
from personal_agent.governance.sub_agent_tools import (
    SUB_AGENT_DENIED_MODES,
    SubAgentToolGrant,
    evaluate_sub_agent_tool_grant,
)
from personal_agent.llm_client.types import ModelRole
from personal_agent.observability.topology import report_degradation
from personal_agent.orchestrator.expansion_types import (
    ExpansionPhase,
    ExpansionPlan,
    PhaseResult,
    PlanTask,
    SubAgentInterval,
)
from personal_agent.orchestrator.fallback_planner import generate_fallback_plan
from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec
from personal_agent.orchestrator.tool_dispatch import get_shared_tool_execution_layer
from personal_agent.orchestrator.worker_types import (
    THOROUGHNESS_LEVELS,
    WORKER_TYPES,
    WorkerType,
    render_worker_report_body,
    worker_types_declaring,
)

logger = structlog.get_logger(__name__)

# Plan schema: max tasks per strategy. ADR-0150 D4: no reserved extra slot — the
# combine task it held re-did the research with none of the sibling reports, and
# the primary synthesises over every result anyway.
_MAX_TASKS = {"HYBRID": 3, "DECOMPOSE": 5}
# FRE-1389: bound on how many out-of-grant gap signals one dispatch pass acts
# on — a defensive cap, not an expected count (a result's own signals are
# already capped at the source; this bounds the union across every task).
_MAX_GAP_NAMES_PER_TASK = 10


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
        The complete planner system prompt.
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
    levels = ", ".join(
        f"{level} ({get_settings().sub_agent_rounds_for(level)} tool round(s))"
        for level in THOROUGHNESS_LEVELS
    )
    type_names = "|".join(t.value for t in WORKER_TYPES)
    level_names = "|".join(THOROUGHNESS_LEVELS)
    return (
        "You are a task decomposition planner. Given a user query and a strategy, "
        "produce a JSON plan that breaks the query into independent sub-tasks.\n\n"
        "Output ONLY valid JSON matching this schema:\n"
        '{"strategy": "HYBRID|DECOMPOSE", "tasks": [{"name": "string", '
        f'"goal": "string", "constraints": ["string"], "type": "{type_names}", '
        f'"thoroughness": "{level_names}"}}]}}\n\n'
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
        "- Do NOT answer the question — only produce the plan"
    )


@dataclass
class ExpansionResult:
    """Complete result of an expansion controller execution.

    Attributes:
        plan: The expansion plan (LLM-generated or fallback).
        sub_agent_results: Results from all dispatched sub-agents.
        synthesis_context: Formatted string for the synthesis LLM call.
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
            came up in the serialized loop. Distinct from a failed
            ``SubAgentResult`` — these never ran at all, so they are reported
            here rather than fabricated into ``sub_agent_results`` (AC-3
            mirrors why FRE-1380 deleted ``_not_admitted_result``).
    """

    plan: ExpansionPlan | None = None
    sub_agent_results: list[SubAgentResult] = field(default_factory=list)
    synthesis_context: str = ""
    phase_results: list[PhaseResult] = field(default_factory=list)
    degraded: bool = False
    degradation_reason: str | None = None
    planner_cost_usd: float = 0.0
    dispatch_intervals: list[SubAgentInterval] = field(default_factory=list)
    skipped_tasks: list[str] = field(default_factory=list)

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
        result.synthesis_context = self._build_synthesis_context(
            plan=plan,
            sub_results=sub_results,
            skipped_tasks=result.skipped_tasks,
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

        Returns:
            An ExpansionPlan — either LLM-generated or fallback.
        """
        start_ms = time.monotonic() * 1000

        logger.info("planner_started", strategy=strategy, trace_id=trace_id)

        # Read once, inside the try (an unexpected lookup error still reaches the
        # fallback), and shared with the fallback planner so both plan against the
        # same grant surface. Empty until read, which fails closed to `general`.
        tool_surface: list[str] = []
        try:
            tool_surface = _current_sub_agent_tool_surface(trace_id)
            planner_system_prompt = _build_planner_system_prompt(tool_surface)
            planner_messages = [
                {"role": "system", "content": planner_system_prompt},
                {
                    "role": "user",
                    "content": (f"Strategy: {strategy}\nQuery: {query}\n\nProduce the JSON plan."),
                },
            ]

            from personal_agent.telemetry.trace import TraceContext

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
                    response_format={"type": "json_object"},
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
            # FRE-501: capture planner-call cost so the executor can roll it into
            # the live turn meter. Paid/cloud calls populate cost_usd; 0.0 otherwise.
            result.planner_cost_usd = float(raw_response.get("cost_usd") or 0.0)
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
                    trace_id=trace_id,
                )
                return plan

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
            logger.warning(
                "planner_failed",
                reason="timeout",
                trace_id=trace_id,
            )

        except Exception as exc:
            logger.warning(
                "planner_failed",
                reason="exception",
                error=str(exc),
                trace_id=trace_id,
            )

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
                task=task.goal,
                context=messages[-4:] if messages else [],
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

        async with phase_span(
            session_id=session_id,
            phase=Phase.EXPANSION,
            detail=f"{len(specs)} sub-agents",
        ) as _parent_id:
            for task, spec in zip(plan.tasks, specs, strict=True):
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
                    turn_deadline_monotonic=turn_deadline_monotonic,
                    user_id=user_id,
                    authenticated=authenticated,
                )
                if replacement is not None:
                    sub_results.append(replacement)

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
            task=f"{task.goal} (retry: {target_type.value} worker)",
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
        skipped_tasks: list[str] | None = None,
    ) -> str:
        """Build the synthesis context string from sub-agent results.

        Args:
            plan: The expansion plan used for this run.
            sub_results: Results from all dispatched sub-agents.
            skipped_tasks: Plan task names never dispatched because the turn's
                budget ran out first (FRE-1397) — distinct from a failure:
                these produced no result at all, so they get their own note
                rather than being silently absent.

        Returns:
            Formatted synthesis context string for the parent agent.
        """
        parts = [f"## Expansion Results (strategy: {plan.strategy})\n\n"]

        n_ok = 0
        n_ledger = 0
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
            if r.success:
                n_ok += 1
            elif r.report_kind == "ledger":
                n_ledger += 1

        if all_gaps:
            parts.append("### Not found\n")
            parts.extend(
                f"- [{task_name}] {looked_for} (looked: {where})\n"
                for task_name, looked_for, where in all_gaps
            )
            parts.append("\n")

        if any(not r.success for r in sub_results):
            failed = [r.spec_task for r in sub_results if not r.success]
            parts.append(
                f"\n**Note:** The following sub-tasks failed: {', '.join(failed)}. "
                "Synthesize from available results and note any gaps.\n"
            )

        if skipped_tasks:
            parts.append(
                f"\n**Note:** The following sub-tasks were not run — the turn's time "
                f"budget was exhausted before dispatch reached them: {', '.join(skipped_tasks)}. "
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

    return ExpansionPlan(
        strategy=data.get("strategy", strategy),
        tasks=tasks,
        is_fallback=False,
    )
