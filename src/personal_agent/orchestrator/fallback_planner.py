"""Deterministic fallback planner for expansion controller.

Generates an ExpansionPlan from prompt structure when the LLM planner
fails (timeout, schema validation failure, empty plan). Scoped to
prompts with explicitly enumerated entities or dimensions.

For open-ended prompts without enumerable structure, produces a generic
2-task split (research + recommendation).

See: ADR-0036 Decision 3 (scoped to enumerated comparisons)
"""

from __future__ import annotations

import re

import structlog

from personal_agent.orchestrator.expansion_types import (
    ExpansionPlan,
    PlanTask,
)

logger = structlog.get_logger(__name__)

# Patterns for extracting enumerated entities from prompts
_COMMA_LIST_RE = re.compile(
    r"(?:compare|evaluate|analyze|assess|review|benchmark)\s+"
    r"([\w\s]+(?:,\s*[\w\s]+)+(?:,?\s*(?:and|or)\s+[\w\s]+)?)",
    re.IGNORECASE,
)

_VS_RE = re.compile(
    r"([\w\s]+?)\s+(?:vs\.?|versus)\s+([\w\s]+)",
    re.IGNORECASE,
)

# Max tasks per strategy
_MAX_HYBRID_TASKS = 3
_MAX_DECOMPOSE_TASKS = 5


def generate_fallback_plan(
    query: str,
    strategy: str,
) -> ExpansionPlan:
    """Generate a deterministic plan from prompt structure.

    Args:
        query: The user's original query text.
        strategy: "HYBRID" or "DECOMPOSE".

    Returns:
        ExpansionPlan with is_fallback=True.
    """
    entities = _extract_entities(query)

    if entities:
        tasks = _build_entity_tasks(entities, query, strategy)
    else:
        tasks = _build_generic_tasks(query, strategy)

    plan = ExpansionPlan(
        strategy=strategy,
        tasks=tasks,
        is_fallback=True,
    )

    logger.info(
        "fallback_plan_generated",
        strategy=strategy,
        task_count=len(tasks),
        entities_found=len(entities),
        entity_names=[e.strip() for e in entities],
    )

    return plan


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
        # Split on commas and "and"/"or"
        parts = re.split(r",\s*|\s+and\s+|\s+or\s+", raw)
        entities = [p.strip() for p in parts if p.strip()]
        if len(entities) >= 2:
            return entities

    # Try "X vs Y" pattern
    match = _VS_RE.search(query)
    if match:
        return [match.group(1).strip(), match.group(2).strip()]

    return []


def _build_entity_tasks(
    entities: list[str],
    query: str,
    strategy: str,
) -> list[PlanTask]:
    """Build tasks from extracted entities.

    Args:
        entities: Extracted entity names.
        query: Original query for context.
        strategy: HYBRID or DECOMPOSE.

    Returns:
        List of PlanTask instances.
    """
    max_tasks = _MAX_HYBRID_TASKS if strategy == "HYBRID" else _MAX_DECOMPOSE_TASKS
    entity_tasks = entities[:max_tasks]

    tasks: list[PlanTask] = []
    for entity in entity_tasks:
        tasks.append(
            PlanTask(
                name=f"evaluate_{_slugify(entity)}",
                goal=f"Evaluate {entity} in the context of: {query}",
                constraints=[
                    f"Focus specifically on {entity}",
                    "Include strengths, weaknesses, and trade-offs",
                ],
                expected_output="Evaluation summary with key findings",
            )
        )

    # Add synthesis/recommendation task
    entity_list = ", ".join(entity_tasks)
    tasks.append(
        PlanTask(
            name="synthesize_recommendation",
            goal=f"Synthesize findings across {entity_list} and provide a recommendation",
            constraints=[
                "Reference specific findings from sub-agent evaluations",
                "Provide a clear recommendation with reasoning",
            ],
            expected_output="Comparative synthesis with recommendation",
        )
    )

    return tasks


def _build_generic_tasks(query: str, strategy: str) -> list[PlanTask]:
    """Build generic 2-task split for prompts without enumerable structure.

    Args:
        query: Original query text.
        strategy: HYBRID or DECOMPOSE.

    Returns:
        Two-task plan: research/analysis + recommendation/synthesis.
    """
    return [
        PlanTask(
            name="research_analysis",
            goal=f"Research and analyze: {query}" if query else "Research the topic",
            constraints=["Be thorough but focused", "Identify key considerations"],
            expected_output="Analysis with key findings",
        ),
        PlanTask(
            name="synthesize_recommendation",
            goal="Synthesize the research into a clear recommendation",
            constraints=[
                "Reference specific findings from the analysis",
                "Provide actionable guidance",
            ],
            expected_output="Recommendation with supporting evidence",
        ),
    ]


def _slugify(text: str) -> str:
    """Convert text to a safe identifier slug."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40]
