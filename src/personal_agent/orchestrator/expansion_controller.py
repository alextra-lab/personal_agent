"""Expansion controller — deterministic workflow enforcement.

When the gateway sets strategy ∈ {HYBRID, DECOMPOSE} and orchestration_mode
is "enforced", this controller takes over from the executor. The LLM generates
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
from dataclasses import dataclass, field
from typing import Any

import structlog

from personal_agent.config import get_settings
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

logger = structlog.get_logger(__name__)

# Plan schema: max entity tasks per strategy (synthesis task is additional)
_MAX_TASKS = {"HYBRID": 4, "DECOMPOSE": 6}

# System prompt for the planner LLM call
_PLANNER_SYSTEM_PROMPT = (
    "You are a task decomposition planner. Given a user query and a strategy, "
    "produce a JSON plan that breaks the query into independent sub-tasks.\n\n"
    "Output ONLY valid JSON matching this schema:\n"
    '{"strategy": "HYBRID|DECOMPOSE", "tasks": [{"name": "string", '
    '"goal": "string", "constraints": ["string"], "expected_output": "string"}]}\n\n'
    "Rules:\n"
    "- Each task must be independently answerable\n"
    "- HYBRID: 2-3 tasks + 1 synthesis task (max 4)\n"
    "- DECOMPOSE: 3-5 tasks + 1 recommendation task (max 6)\n"
    "- task names must be snake_case identifiers\n"
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
    """

    plan: ExpansionPlan | None = None
    sub_agent_results: list[SubAgentResult] = field(default_factory=list)
    synthesis_context: str = ""
    phase_results: list[PhaseResult] = field(default_factory=list)
    degraded: bool = False
    degradation_reason: str | None = None
    planner_cost_usd: float = 0.0
    dispatch_intervals: list[SubAgentInterval] = field(default_factory=list)

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


class ExpansionController:
    """Deterministic expansion enforcement.

    Usage:
        controller = ExpansionController()
        result = await controller.execute(query, strategy, llm_client, trace_id, messages)
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
            planner_messages = [
                {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
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
                    max_tokens=1024,
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

            logger.warning(
                "planner_failed",
                reason="schema_validation_failed",
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

        Returns:
            List of SubAgentResult, in dispatch order — one entry per task that
            returned a result (success or a reported per-task failure). A task
            whose dispatch raised a raw exception is dropped from this list; see
            ``result.dispatch_intervals`` for the complete per-task record,
            including dropped tasks.
        """
        settings = get_settings()
        start_ms = time.monotonic() * 1000

        logger.info(
            "expansion_dispatch_started",
            task_count=len(plan.tasks),
            trace_id=trace_id,
        )

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
                # setting is still used by the separate autonomous-mode
                # decomposition path in expansion.py, which is why it is not
                # deleted, just no longer read here.
                timeout_seconds=settings.worker_timeout_seconds,
                hard_deadline_seconds=settings.worker_hard_deadline_seconds,
                tools=task.tools,
                background=(f"Sub-task: {task.name}. Constraints: {', '.join(task.constraints)}"),
                mode=task.mode,
            )
            for task in plan.tasks
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

        async with phase_span(
            session_id=session_id,
            phase=Phase.EXPANSION,
            detail=f"{len(specs)} sub-agents",
        ) as _parent_id:
            for task, spec in zip(plan.tasks, specs, strict=True):
                interval_start = time.monotonic()
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
                        )
                except Exception as exc:
                    raw_results.append(exc)
                else:
                    raw_results.append(sub_result)
                finally:
                    intervals.append(SubAgentInterval(task.name, interval_start, time.monotonic()))

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

        # Filter out exceptions — keep every SubAgentResult (success or per-task
        # failure are both real, reportable outcomes; only a raw exception is
        # dropped here, matching the prior gather(return_exceptions=True) filter).
        sub_results: list[SubAgentResult] = [
            r for r in raw_results if isinstance(r, SubAgentResult)
        ]
        failed_count = len(raw_results) - len(sub_results)
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

    def _build_synthesis_context(
        self,
        plan: ExpansionPlan,
        sub_results: list[SubAgentResult],
    ) -> str:
        """Build the synthesis context string from sub-agent results.

        Args:
            plan: The expansion plan used for this run.
            sub_results: Results from all dispatched sub-agents.

        Returns:
            Formatted synthesis context string for the parent agent.
        """
        parts = [f"## Expansion Results (strategy: {plan.strategy})\n\n"]

        for r in sub_results:
            status = "OK" if r.success else f"FAILED: {r.error}"
            parts.append(f"### {r.spec_task} [{status}]\n{r.summary}\n\n")

        if any(not r.success for r in sub_results):
            failed = [r.spec_task for r in sub_results if not r.success]
            parts.append(
                f"\n**Note:** The following sub-tasks failed: {', '.join(failed)}. "
                "Synthesize from available results and note any gaps.\n"
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

        tasks.append(
            PlanTask(
                name=str(name),
                goal=str(goal),
                constraints=[str(c) for c in t.get("constraints", [])],
                expected_output=str(t.get("expected_output", "text")),
            )
        )

    if not tasks:
        return None

    return ExpansionPlan(
        strategy=data.get("strategy", strategy),
        tasks=tasks,
        is_fallback=False,
    )
