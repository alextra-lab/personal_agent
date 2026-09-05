"""HYBRID expansion orchestration.

When the gateway flags HYBRID or DECOMPOSE, the primary agent creates
a decomposition plan, this module parses it into SubAgentSpecs, runs
them sequentially (FRE-1381, matching FRE-1380's serialization of the
enforced-mode dispatch path), and returns results for the primary
agent to synthesize.

Gateway decides IF to expand. Agent decides HOW. This module does the HOW.

Sub-agent client isolation (ADR-0033): execute_hybrid() creates its own client
via get_llm_client("sub_agent") — sub-agents always use the sub_agent model
config, never inheriting the primary agent's client.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.4
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence

import structlog

from personal_agent.config import settings
from personal_agent.orchestrator.expansion_types import SubAgentInterval
from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec

logger = structlog.get_logger(__name__)

_NUMBERED_ITEM_RE = re.compile(r"^\s*\d+[\.\)]\s*(.+)", re.MULTILINE)


def parse_decomposition_plan(
    plan_text: str,
    max_sub_agents: int = 3,
    default_max_tokens: int | None = None,
    default_timeout: float | None = None,
) -> list[SubAgentSpec]:
    """Parse a primary agent's decomposition plan into SubAgentSpecs.

    The plan is expected to be a numbered list of tasks. Each item
    becomes a separate SubAgentSpec with default parameters.

    Args:
        plan_text: The primary agent's decomposition plan (numbered list).
        max_sub_agents: Maximum specs to produce.
        default_max_tokens: Token budget per sub-agent (None = config default).
        default_timeout: Timeout per sub-agent (None = config default).

    Returns:
        List of SubAgentSpecs, one per plan item (up to max_sub_agents).
    """
    matches = _NUMBERED_ITEM_RE.findall(plan_text)
    if not matches:
        return []

    max_tokens = default_max_tokens or settings.sub_agent_max_tokens
    timeout = default_timeout or settings.sub_agent_timeout_seconds

    specs: list[SubAgentSpec] = []
    for task_text in matches[:max_sub_agents]:
        task_text = task_text.strip()
        if not task_text:
            continue
        specs.append(
            SubAgentSpec(
                task=task_text,
                context=[],  # Primary agent will enrich context
                output_format="markdown_summary",
                max_tokens=max_tokens,
                timeout_seconds=timeout,
            )
        )

    return specs


async def execute_hybrid(
    specs: Sequence[SubAgentSpec],
    trace_id: str,
    session_id: str | None = None,
    eval_mode: bool = False,
) -> list[SubAgentResult]:
    """Execute sub-agents sequentially, one task at a time.

    Creates a dedicated sub_agent LLM client via factory (ADR-0033 client isolation).
    Sub-agents always use the sub_agent model config — they never inherit the primary
    agent's client or model.

    FRE-1381 (matching FRE-1380's serialization of the enforced-mode dispatch path):
    dispatch is sequential, not concurrent. Sub-agents exist for context isolation —
    a digest reaches synthesis, never the full transcript — and that property holds
    identically whether tasks run side by side or one after another. All sub-agents
    still run; partial failures do not abort the batch.

    Args:
        specs: Sub-agent specifications from decomposition planning.
        trace_id: Parent request trace identifier.
        session_id: Originating session id for cost attribution (ADR-0074).
        eval_mode: True when the parent turn originated from an eval run; threaded
            to per-sub-agent audit records for EVAL provenance (FRE-523).

    Returns:
        List of SubAgentResults, in dispatch order, whether they succeeded or
        failed during execution.
    """
    from personal_agent.llm_client.factory import get_llm_client

    # Sub-agent client isolation: always use "sub_agent" role config (ADR-0033)
    sub_agent_client = get_llm_client(role_name="sub_agent")

    logger.info(
        "hybrid_expansion_start",
        sub_agent_count=len(specs),
        trace_id=trace_id,
    )

    dispatch_start = time.monotonic()
    raw_results: list[SubAgentResult | Exception] = []
    intervals: list[SubAgentInterval] = []

    for spec in specs:
        interval_start = time.monotonic()
        try:
            sub_result = await run_sub_agent(
                spec=spec,
                llm_client=sub_agent_client,
                trace_id=trace_id,
                session_id=session_id,
                eval_mode=eval_mode,
            )
        except Exception as exc:
            raw_results.append(exc)
        else:
            raw_results.append(sub_result)
        finally:
            intervals.append(SubAgentInterval(spec.task[:80], interval_start, time.monotonic()))

    logger.info(
        "hybrid_expansion_intervals",
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
    # failure are both real, reportable outcomes; only a raw exception, e.g. from
    # setup code that ran before run_sub_agent's own try block, is dropped here).
    results: list[SubAgentResult] = [r for r in raw_results if isinstance(r, SubAgentResult)]

    successes = sum(1 for r in results if r.success)
    failures = len(results) - successes

    logger.info(
        "hybrid_expansion_complete",
        total=len(results),
        successes=successes,
        failures=failures,
        trace_id=trace_id,
    )

    return results
