"""Deterministic fallback planner for expansion controller.

Generates an ExpansionPlan from prompt structure when the LLM planner
fails (timeout, schema validation failure, empty plan). Scoped to
prompts with explicitly enumerated entities or dimensions.

For open-ended prompts without enumerable structure, produces a single
research task.

No task combines the others' results (ADR-0150 D4): the primary synthesises
over every worker's report, so a combine task only re-does the research.

See: ADR-0036 Decision 3 (scoped to enumerated comparisons)
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import structlog

from personal_agent.orchestrator.expansion_types import (
    ExpansionPlan,
    PlanTask,
)
from personal_agent.orchestrator.worker_types import WORKER_TYPES, WorkerType

logger = structlog.get_logger(__name__)

# Patterns for extracting enumerated entities from prompts
_COMMA_LIST_RE = re.compile(
    r"(?:compare|evaluate|analyze|assess|review|benchmark)\s+"
    r"([\w\s]+(?:,\s*[\w\s]+)+(?:,?\s*(?:and|or)\s+[\w\s]+)?)",
    re.IGNORECASE,
)

_VS_RE = re.compile(
    r"\b([\w\-\.]+)\s+(?:vs\.?|versus)\s+([\w\-\.]+)",
    re.IGNORECASE,
)

# Strip trailing prepositional phrases from extracted entities
_TRAILING_PREP_RE = re.compile(
    r"\s+(?:for|in|on|of|across|with|to|about|from|using|against)\s+.*$",
    re.IGNORECASE,
)

# Max tasks per strategy
_MAX_HYBRID_TASKS = 3
_MAX_DECOMPOSE_TASKS = 5


def generate_fallback_plan(
    query: str,
    strategy: str,
    sub_agent_tool_surface: Sequence[str] = (),
    max_tasks: int | None = None,
) -> ExpansionPlan:
    """Generate a deterministic plan from prompt structure.

    Args:
        query: The user's original query text.
        strategy: "HYBRID" or "DECOMPOSE".
        sub_agent_tool_surface: Tool names grantable to a sub-agent in the
            current mode. Every task is a ``researcher`` when a researcher tool
            is in it, else a ``general`` worker (ADR-0150 D2). Empty — the
            default, and what a failed governance lookup yields — fails closed to
            ``general``.
        max_tasks: The turn's own per-turn expansion budget (FRE-1382) —
            tightens the strategy cap, never relaxes it. A negative value is
            treated as zero (an empty plan degrades the turn back to the
            primary loop, never a widened cap from Python's negative-index
            slicing). ``None`` (the default) leaves the strategy cap as the
            only bound.

    Returns:
        ExpansionPlan with is_fallback=True.
    """
    worker_type = _fallback_worker_type(sub_agent_tool_surface)
    entities = _extract_entities(query)
    strategy_cap = _MAX_HYBRID_TASKS if strategy == "HYBRID" else _MAX_DECOMPOSE_TASKS
    effective_cap = strategy_cap if max_tasks is None else max(0, min(strategy_cap, max_tasks))

    if entities:
        tasks = _build_entity_tasks(entities, query, strategy, worker_type)
    else:
        tasks = _build_generic_tasks(query, worker_type)
    # Applied uniformly to whichever branch ran, rather than threading the cap
    # into each builder separately — a single choke point neither builder can
    # bypass (FRE-1382: the generic single-task branch used to ignore it).
    tasks = tasks[:effective_cap]

    plan = ExpansionPlan(
        strategy=strategy,
        tasks=tasks,
        is_fallback=True,
    )

    logger.info(
        "fallback_plan_generated",
        strategy=strategy,
        task_count=len(tasks),
        worker_type=worker_type.value,
        entities_found=len(entities),
        entity_names=[e.strip() for e in entities],
    )

    return plan


def _fallback_worker_type(sub_agent_tool_surface: Sequence[str]) -> WorkerType:
    """``researcher`` when any of its tools is grantable now, else ``general``."""
    researcher_tools = WORKER_TYPES[WorkerType.RESEARCHER].tools
    if any(tool in sub_agent_tool_surface for tool in researcher_tools):
        return WorkerType.RESEARCHER
    return WorkerType.GENERAL


def _clean_entity(raw: str) -> str:
    """Strip trailing prepositional phrases from an extracted entity name."""
    cleaned = _TRAILING_PREP_RE.sub("", raw).strip()
    return cleaned if cleaned else raw.strip()


def _extract_entities(query: str) -> list[str]:
    """Extract enumerated entities or dimensions from the query.

    Looks for comma-separated lists and "X vs Y" patterns.

    Args:
        query: User query text.

    Returns:
        List of extracted entity/dimension names. Empty if none found.
    """
    # Try comma-list pattern first: "Compare Redis, Memcached, and Hazelcast"
    match = _COMMA_LIST_RE.search(query)
    if match:
        raw = match.group(1)
        # Split on commas (optionally followed by "and"/"or") and standalone "and"/"or"
        parts = re.split(r",\s*(?:and\s+|or\s+)?|\s+and\s+|\s+or\s+", raw)
        entities = [_clean_entity(p) for p in parts if p.strip()]
        if len(entities) >= 2:
            return entities

    # Try "X vs Y" pattern
    match = _VS_RE.search(query)
    if match:
        return [_clean_entity(match.group(1)), _clean_entity(match.group(2))]

    return []


def _build_entity_tasks(
    entities: list[str],
    query: str,
    strategy: str,
    worker_type: WorkerType,
) -> list[PlanTask]:
    """Build one task per extracted entity.

    Args:
        entities: Extracted entity names.
        query: Original query for context.
        strategy: HYBRID or DECOMPOSE.
        worker_type: The registry type every task runs as.

    Returns:
        List of PlanTask instances.
    """
    max_tasks = _MAX_HYBRID_TASKS if strategy == "HYBRID" else _MAX_DECOMPOSE_TASKS
    return [
        PlanTask(
            name=f"evaluate_{_slugify(entity)}",
            goal=f"Evaluate {entity} in the context of: {query}",
            type=worker_type,
            thoroughness=WORKER_TYPES[worker_type].default_thoroughness,
            constraints=[
                f"Focus specifically on {entity}",
                "Include strengths, weaknesses, and trade-offs",
            ],
        )
        for entity in entities[:max_tasks]
    ]


def _build_generic_tasks(query: str, worker_type: WorkerType) -> list[PlanTask]:
    """Build the single research task for a prompt without enumerable structure.

    Args:
        query: Original query text.
        worker_type: The registry type the task runs as.

    Returns:
        A one-task plan.
    """
    return [
        PlanTask(
            name="research_analysis",
            goal=f"Research and analyze: {query}" if query else "Research the topic",
            type=worker_type,
            thoroughness=WORKER_TYPES[worker_type].default_thoroughness,
            constraints=["Be thorough but focused", "Identify key considerations"],
        ),
    ]


def _slugify(text: str) -> str:
    """Convert text to a safe identifier slug."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40]
