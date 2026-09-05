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
from typing import Any

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

logger = structlog.get_logger(__name__)

# Plan schema: max entity tasks per strategy (synthesis task is additional)
_MAX_TASKS = {"HYBRID": 4, "DECOMPOSE": 6}
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
    return list(governance_config.sub_agent_tools)


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
    if available_sub_agent_tools:
        tools_rule = (
            "list of tool names this task needs, chosen ONLY from: "
            f"{', '.join(available_sub_agent_tools)} (omit or leave empty if none needed)"
        )
    else:
        tools_rule = "no tools are currently available to sub-agents — always omit or leave empty"
    return (
        "You are a task decomposition planner. Given a user query and a strategy, "
        "produce a JSON plan that breaks the query into independent sub-tasks.\n\n"
        "Output ONLY valid JSON matching this schema:\n"
        '{"strategy": "HYBRID|DECOMPOSE", "tasks": [{"name": "string", '
        '"goal": "string", "constraints": ["string"], "expected_output": "string", '
        '"tools": ["string"]}]}\n\n'
        "Rules:\n"
        "- Each task must be independently answerable\n"
        "- HYBRID: 2-3 tasks + 1 synthesis task (max 4)\n"
        "- DECOMPOSE: 3-5 tasks + 1 recommendation task (max 6)\n"
        "- task names must be snake_case identifiers\n"
        f"- tools: {tools_rule}\n"
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
    """Filter each task's requested tools against the sub-agent tool grant set (FRE-1388).

    Fails safe: a governance-config or mode-lookup error denies every tool this
    dispatch requested rather than aborting the whole expansion turn. The grant
    set's own policy is already default-deny, so degrading to "deny everything"
    on a lookup failure changes no correctness guarantee — it only matters once
    a task actually requests a tool, which the planner never does today (FRE-884).

    Args:
        tasks: Plan tasks whose ``tools`` field to filter.
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
                denied=tuple(task.tools),
                denial_reason=f"governance lookup failed: {exc}",
            )
            for task in tasks
        ]

    grants = [
        evaluate_sub_agent_tool_grant(task.tools, current_mode, governance_config) for task in tasks
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

        Returns:
            An ExpansionPlan — either LLM-generated or fallback.
        """
        start_ms = time.monotonic() * 1000

        logger.info("planner_started", strategy=strategy, trace_id=trace_id)

        try:
            planner_system_prompt = _build_planner_system_prompt(
                _current_sub_agent_tool_surface(trace_id)
            )
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
                    # a bad plan. SUB_AGENT binds to the instruct sibling with
                    # thinking hard-disabled (config/model_roles.yaml); PRIMARY
                    # is the thinking-capable deployment the plan's own output
                    # will be judged against.
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
                    trace_ctx=TraceContext(trace_id=trace_id, session_id=session_id),
                ),
                timeout=timeout_s,
            )

            duration_ms = time.monotonic() * 1000 - start_ms
            # FRE-501: capture planner-call cost so the executor can roll it into
            # the live turn meter. Paid/cloud calls populate cost_usd; 0.0 otherwise.
            result.planner_cost_usd = float(raw_response.get("cost_usd") or 0.0)
            plan = _validate_plan_json(raw_response["content"], strategy)

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
        fallback_plan = generate_fallback_plan(query=query, strategy=strategy)
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
        settings = get_settings()
        start_ms = time.monotonic() * 1000

        logger.info(
            "expansion_dispatch_started",
            task_count=len(plan.tasks),
            trace_id=trace_id,
        )

        # FRE-1388: a sub-agent is a distinct governance principal from the
        # primary. task.tools is model-authored (the planner's output) and is
        # filtered against the sub-agent tool grant set here — never passed
        # through unfiltered, and never checked against the primary's own
        # per-tool `allowed_in_modes`.
        grants = _compute_sub_agent_grants(plan.tasks, trace_id)

        specs = [
            SubAgentSpec(
                task=task.goal,
                context=messages[-4:] if messages else [],
                output_format=task.expected_output,
                # FRE-1379: no max_tokens override here — SubAgentSpec's own
                # default (None) defers to the deployment's catalog-declared
                # ceiling. settings.sub_agent_max_tokens used to be passed here
                # unconditionally and silently shadowed the catalog's own
                # (smaller, deliberately sized) value on every call; that
                # setting's only other reader was the autonomous-mode
                # decomposition path, deleted in FRE-1381 along with the field.
                timeout_seconds=settings.worker_timeout_seconds,
                hard_deadline_seconds=settings.worker_hard_deadline_seconds,
                tools=list(grant.granted),
                background=(f"Sub-task: {task.name}. Constraints: {', '.join(task.constraints)}"),
                mode=task.mode,
                denied_tools=grant.denied,
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
    ) -> SubAgentResult | None:
        """Single-shot replacement dispatch when a sub-agent stated a tool gap (FRE-1389 AC-5).

        The sub-agent only ever REPORTS a gap — an out-of-grant tool-call
        attempt refused at the source (``refused_tool_attempts``), or its own
        ``TOOL_GAP: <name>`` sentinel (``stated_tool_gap``) — it never
        acquires the tool itself. This method, acting on the controller's
        behalf (the "primary" in the ticket's architecture section), is the
        only thing that may construct a replacement with an expanded grant,
        and it does so at most once per task: the replacement's own gap
        signals, if any, are never checked, so a task cannot chain retries.

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

        Returns:
            The replacement SubAgentResult, or ``None`` when there was no gap,
            the gap named nothing actually registered, a re-check of the
            grant still denies it (the original refusal already stands via
            ``denied_tools``/the synthesis context), or the turn's budget is
            already exhausted.
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

        # Reuses _compute_sub_agent_grants's existing fail-closed lookup
        # (GovernanceConfigError/ModeManagerError → deny everything) rather
        # than a second hand-rolled try/except around the same lookup.
        (new_grant,) = _compute_sub_agent_grants(
            [replace(task, tools=list(dict.fromkeys([*task.tools, *gap_names])))], trace_id
        )
        # Compare against spec.tools (what was ACTUALLY granted before this
        # retry), not task.tools (what the planner merely requested) — granted
        # is always a subset of requested, so a name denied in the original
        # request would otherwise be excluded from this check for free and
        # mask a real expansion on the rare case governance state itself
        # changes between the original dispatch and this redispatch check.
        newly_grantable = set(new_grant.granted) - set(spec.tools)
        if not newly_grantable:
            return None

        logger.info(
            "sub_agent_redispatched_with_expanded_grant",
            task_name=task.name,
            stated_gap=gap_names,
            expanded_grant=list(new_grant.granted),
            trace_id=trace_id,
        )

        replacement_spec = replace(
            spec,
            task=f"{task.goal} (retry: expanded tool grant)",
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

        for r in sub_results:
            status = "OK" if r.success else f"FAILED: {r.error}"
            parts.append(f"### {r.spec_task} [{status}]\n{r.summary}\n\n")
            if r.denied_tools:
                # FRE-1388 AC-4: denial is deterministic and lands in the report
                # itself, not only a log line — the recovery path is the primary
                # re-planning with a different grant, so it must see this here.
                parts.append(
                    f"*Tool access denied:* {', '.join(r.denied_tools)} was requested "
                    "but not granted to sub-agents; this sub-task ran without it.\n\n"
                )

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

        return "".join(parts)


def _validate_plan_json(
    raw: str,
    strategy: str = "HYBRID",
) -> ExpansionPlan | None:
    """Validate LLM output against the plan schema.

    Args:
        raw: Raw string from the LLM planner response.
        strategy: Expected strategy — used as fallback if not in JSON.

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

    max_tasks = _MAX_TASKS.get(strategy, 4)
    tasks: list[PlanTask] = []

    for t in tasks_raw[: max_tasks + 1]:  # +1 for synthesis/recommendation task
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        goal = t.get("goal")
        if not name or not goal:
            return None

        raw_tools = t.get("tools", [])
        tools = [str(x) for x in raw_tools] if isinstance(raw_tools, list) else []

        tasks.append(
            PlanTask(
                name=str(name),
                goal=str(goal),
                constraints=[str(c) for c in t.get("constraints", [])],
                expected_output=str(t.get("expected_output", "text")),
                tools=tools,
            )
        )

    if not tasks:
        return None

    return ExpansionPlan(
        strategy=data.get("strategy", strategy),
        tasks=tasks,
        is_fallback=False,
    )
