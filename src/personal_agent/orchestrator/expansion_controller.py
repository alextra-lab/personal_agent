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
from personal_agent.orchestrator.expansion_types import (
    ExpansionPhase,
    ExpansionPlan,
    PhaseResult,
    PlanTask,
    SubAgentMode,
)
from personal_agent.orchestrator.fallback_planner import generate_fallback_plan
from personal_agent.orchestrator.sub_agent import _DISCOVERY_TOOL_ALLOWLIST, run_sub_agent
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

# ADR-0086 D3 — discovery-slice augmentation, appended ONLY when
# artifact_decomposition_enabled is on. Built per-call (never by mutating the
# constant above) so flag-on never leaks into later calls. Existing HYBRID turns
# stay byte-for-byte unchanged while the flag is off.
_PLANNER_DISCOVERY_SLICE_GUIDANCE = (
    "\n\nFor artifact-build queries (e.g. 'build an interactive HTML guide that "
    "explains X'), decompose the DISCOVERY work into non-overlapping investigation "
    "slices that can run concurrently. Each discovery slice may add two fields:\n"
    '  "mode": "tooled_sequential",\n'
    '  "tools": [<read-only discovery tools>]\n'
    f"Allowed read-only tools: {', '.join(sorted(_DISCOVERY_TOOL_ALLOWLIST))}. "
    "Never request write/edit/artifact_write or any mutating tool. Keep slices "
    "non-overlapping so they do not rediscover the same facts."
)


def _build_planner_system_prompt() -> str:
    """Build the planner system prompt, flag-gated for discovery slices.

    Returns:
        The base planner prompt, plus the tooled-discovery-slice guidance only
        when ``settings.artifact_decomposition_enabled`` is True (ADR-0086 D3).
        Built per-call so the augmentation never leaks across calls when the flag
        flips.
    """
    if get_settings().artifact_decomposition_enabled:
        return _PLANNER_SYSTEM_PROMPT + _PLANNER_DISCOVERY_SLICE_GUIDANCE
    return _PLANNER_SYSTEM_PROMPT


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
    """

    plan: ExpansionPlan | None = None
    sub_agent_results: list[SubAgentResult] = field(default_factory=list)
    synthesis_context: str = ""
    phase_results: list[PhaseResult] = field(default_factory=list)
    degraded: bool = False
    degradation_reason: str | None = None
    planner_cost_usd: float = 0.0

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
    ) -> ExpansionResult:
        """Run the full expansion pipeline.

        Args:
            query: User's original query.
            strategy: "HYBRID" or "DECOMPOSE".
            llm_client: LLM client for planner and synthesis calls.
            trace_id: Request trace identifier.
            messages: Conversation context for sub-agents.
            constraints: Optional expansion constraints from gateway.
            session_id: Originating session id for cost attribution (ADR-0074).

        Returns:
            ExpansionResult with plan, sub-agent results, and synthesis context.
        """
        result = ExpansionResult()
        settings = get_settings()

        # --- Phase 1: Planning ---
        plan = await self._run_planner(
            query=query,
            strategy=strategy,
            llm_client=llm_client,
            trace_id=trace_id,
            timeout_s=settings.planner_timeout_seconds,
            result=result,
            session_id=session_id,
        )
        result.plan = plan

        if not plan or not plan.tasks:
            result.degraded = True
            result.degradation_reason = "No valid plan produced"
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
        elif not sub_results:
            result.degraded = True
            result.degradation_reason = "No sub-agent results"

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
                {"role": "system", "content": _build_planner_system_prompt()},
                {
                    "role": "user",
                    "content": (f"Strategy: {strategy}\nQuery: {query}\n\nProduce the JSON plan."),
                },
            ]

            from personal_agent.telemetry.trace import TraceContext

            raw_response = await asyncio.wait_for(
                llm_client.respond(
                    role=ModelRole.SUB_AGENT,
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
                    duration_ms=round(duration_ms),
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
            duration_ms = time.monotonic() * 1000 - start_ms
            logger.warning(
                "planner_failed",
                reason="timeout",
                duration_ms=round(duration_ms),
                trace_id=trace_id,
            )

        except Exception as exc:
            duration_ms = time.monotonic() * 1000 - start_ms
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
    ) -> list[SubAgentResult]:
        """Phase 2: Dispatch sub-agents in parallel.

        Args:
            plan: Validated expansion plan with tasks.
            llm_client: LLM client for sub-agent inference calls.
            trace_id: Request trace identifier.
            messages: Conversation context window slice for sub-agents.
            result: ExpansionResult to append phase data to.
            session_id: Originating session id for cost attribution (ADR-0074).

        Returns:
            List of SubAgentResult from all dispatched sub-agents.
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
                max_tokens=settings.sub_agent_max_tokens,
                timeout_seconds=settings.worker_timeout_seconds,
                tools=task.tools,
                background=(f"Sub-task: {task.name}. Constraints: {', '.join(task.constraints)}"),
                mode=task.mode,
            )
            for task in plan.tasks
        ]

        try:
            raw_results = await asyncio.wait_for(
                asyncio.gather(
                    *[
                        run_sub_agent(
                            spec=spec,
                            llm_client=llm_client,
                            trace_id=trace_id,
                            session_id=session_id,
                        )
                        for spec in specs
                    ],
                    return_exceptions=True,
                ),
                timeout=settings.worker_global_timeout_seconds,
            )
            # Filter out exceptions — keep only successful SubAgentResult objects
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
        except asyncio.TimeoutError:
            # Global timeout cancels all tasks
            logger.warning("expansion_dispatch_global_timeout", trace_id=trace_id)
            sub_results = []
            result.degraded = True
            result.degradation_reason = "Global dispatch timeout"

        duration_ms = time.monotonic() * 1000 - start_ms

        result.phase_results.append(
            PhaseResult(
                phase=ExpansionPhase.DISPATCH,
                duration_ms=duration_ms,
                success=len(sub_results) > 0,
                error="Global timeout" if not sub_results else None,
            )
        )

        for sr in sub_results:
            logger.info(
                "subagent_completed",
                task_name=sr.spec_task,
                duration_ms=round(sr.duration_ms),
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

    # ADR-0086 D3: tooled discovery slices are accepted ONLY when the rollout flag
    # is on. Flag off ⇒ every task is PARALLEL_INFERENCE with no tools, so existing
    # HYBRID/DECOMPOSE turns never enter the tool loop (inertness guarantee).
    discovery_enabled = get_settings().artifact_decomposition_enabled

    for t in tasks_raw[: max_tasks + 1]:  # +1 for synthesis/recommendation task
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        goal = t.get("goal")
        if not name or not goal:
            return None

        mode = SubAgentMode.PARALLEL_INFERENCE
        tools: list[str] = []
        if discovery_enabled and str(t.get("mode", "")).lower() == "tooled_sequential":
            mode = SubAgentMode.TOOLED_SEQUENTIAL
            # Read-only enforcement at the planner boundary: drop any tool outside
            # the discovery allowlist before it ever reaches a sub-agent.
            tools = [
                str(tool)
                for tool in (t.get("tools") or [])
                if str(tool) in _DISCOVERY_TOOL_ALLOWLIST
            ]

        tasks.append(
            PlanTask(
                name=str(name),
                goal=str(goal),
                constraints=[str(c) for c in t.get("constraints", [])],
                expected_output=str(t.get("expected_output", "text")),
                mode=mode,
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
