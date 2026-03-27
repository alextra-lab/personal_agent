"""HYBRID expansion orchestration.

When the gateway flags HYBRID or DECOMPOSE, the primary agent creates
a decomposition plan, this module parses it into SubAgentSpecs, runs
them concurrently (within the expansion_budget), and returns results
for the primary agent to synthesize.

Gateway decides IF to expand. Agent decides HOW. This module does the HOW.

Sub-agent client isolation (ADR-0033): execute_hybrid() creates its own client
via get_llm_client("sub_agent") — sub-agents always use the sub_agent model
config, never inheriting the primary agent's client.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.4
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence

import structlog

from personal_agent.config import settings
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
    max_concurrent: int | None = None,
) -> list[SubAgentResult]:
    """Execute sub-agents concurrently within the expansion budget.

    Creates a dedicated sub_agent LLM client via factory (ADR-0033 client isolation).
    Sub-agents always use the sub_agent model config — they never inherit the primary
    agent's client or model.

    Uses an asyncio.Semaphore to limit concurrent sub-agent calls.
    All sub-agents run; partial failures do not abort the batch.

    Args:
        specs: Sub-agent specifications from decomposition planning.
        trace_id: Parent request trace identifier.
        max_concurrent: Max concurrent sub-agents (None = config default).

    Returns:
        List of SubAgentResults in the same order as specs.
    """
    from personal_agent.llm_client.factory import get_llm_client

    # Sub-agent client isolation: always use "sub_agent" role config (ADR-0033)
    sub_agent_client = get_llm_client(role_name="sub_agent")

    max_conc = max_concurrent or settings.expansion_budget_max
    semaphore = asyncio.Semaphore(max(1, max_conc))

    logger.info(
        "hybrid_expansion_start",
        sub_agent_count=len(specs),
        max_concurrent=max_conc,
        trace_id=trace_id,
    )

    async def _run_with_semaphore(spec: SubAgentSpec) -> SubAgentResult:
        async with semaphore:
            return await run_sub_agent(
                spec=spec,
                llm_client=sub_agent_client,
                trace_id=trace_id,
            )

    tasks = [_run_with_semaphore(spec) for spec in specs]
    results: list[SubAgentResult] = await asyncio.gather(*tasks, return_exceptions=False)

    successes = sum(1 for r in results if r.success)
    failures = len(results) - successes

    logger.info(
        "hybrid_expansion_complete",
        total=len(results),
        successes=successes,
        failures=failures,
        trace_id=trace_id,
    )

    return list(results)
