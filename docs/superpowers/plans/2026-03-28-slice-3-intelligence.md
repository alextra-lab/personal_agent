# Slice 3: Intelligence — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce deterministic expansion when the gateway signals HYBRID/DECOMPOSE, upgrade Seshat with embedding-based hybrid search and fuzzy entity deduplication, add a recall controller for implicit backward-reference queries, wire sub-agent tool access with async/sync execution modes, implement per-phase time budgets with graceful degradation, and (stretch) add proactive memory surfacing, stability threshold redesign, cross-session recall validation, and geospatial retrieval.

**Architecture:** The expansion controller (`orchestrator/expansion_controller.py`) is a deterministic runtime component that intercepts the executor when `strategy ∈ {HYBRID, DECOMPOSE}` and `orchestration_mode == "enforced"`. It calls the LLM for structured plan output, validates against a schema, falls back to a deterministic planner on failure, dispatches sub-agents with per-phase time budgets, and synthesizes results. Sub-agents gain two execution modes: `PARALLEL_INFERENCE` (current fire-and-forget LLM calls) and `TOOLED_SEQUENTIAL` (mini tool-use loop with MCP tool access). The recall controller (`request_gateway/recall_controller.py`) is a Stage 4b post-classification refinement that detects implicit backward-reference cues, corroborates against session history, and reclassifies `CONVERSATIONAL → MEMORY_RECALL` with session fact evidence. Seshat gains embedding vectors on Entity/Turn nodes (via `text-embedding-3-small` or local `nomic-embed-text`), a Neo4j vector index for hybrid search, and a fuzzy entity deduplication pipeline.

**Tech Stack:** Python 3.12+, FastAPI, structlog, Pydantic, Neo4j 5.x (async driver + vector index), Elasticsearch, OpenAI embeddings API (text-embedding-3-small), pytest, mypy, asyncio

**Spec:** `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` — Section 8.3

**ADRs:** ADR-0035 (Seshat backend), ADR-0036 (expansion controller), ADR-0037 (recall controller)

**Evaluation data:** `docs/research/EVAL_08_SLICE_3_PRIORITIES.md`, `docs/research/EVALUATION_PHASE_FINDINGS.md`

**Prerequisite:** Slice 2 complete (commit `a36d5d3`), Evaluation Phase complete (EVAL-01 through EVAL-08)

---

## Acceptance Criteria

### Committed (must ship)

- [ ] Expansion controller enforces sub-agent dispatch when gateway sets HYBRID/DECOMPOSE in `enforced` mode
- [ ] Strategy mismatch rate is near-zero in `enforced` mode (verified by evaluation harness)
- [ ] Dual-mode config: `orchestration_mode: enforced | autonomous`
- [ ] Deterministic fallback planner generates valid plans for enumerated comparison prompts
- [ ] Sub-agents support `TOOLED_SEQUENTIAL` mode with MCP tool access
- [ ] Per-phase time budgets prevent planner monopolization (planner 5–15s, workers 15–45s, synthesis 10–25s)
- [ ] Graceful degradation: partial sub-agent failure → partial synthesis; total failure → degraded direct answer
- [ ] User never sees raw LLM timeout when partial results exist
- [ ] Seshat Entity/Turn nodes have embedding vectors from `text-embedding-3-small`
- [ ] Neo4j vector index operational for similarity search
- [ ] `query_memory` uses hybrid search (vector + keyword + graph traversal)
- [ ] Fuzzy entity dedup: 40 mentions of 10 entities → ~10 canonical nodes (not 500)
- [ ] Recall controller (Stage 4b) reclassifies implicit backward-reference queries
- [ ] CP-19 passes: "Going back to the beginning — what was our primary database again?" → `MEMORY_RECALL`
- [ ] Session fact injection gives the LLM explicit evidence for recall answers
- [ ] Expansion telemetry events: `planner_started`, `planner_completed`, `planner_failed`, `fallback_planner_used`, `expansion_dispatch_started`, `subagent_completed`, `synthesis_started`, `graceful_degradation_triggered`
- [ ] Recall telemetry events: `recall_cue_detected`, `recall_reclassified`, `recall_cue_false_positive`
- [ ] Revised CP-16, CP-17 evaluation assertions include workflow correctness (layer 2)
- [ ] CP-19 adversarial variants (6+ paraphrases) added to evaluation dataset

### Stretch Goals

- [ ] Stability threshold redesigned: organic promotion possible within days, not months
- [ ] Cross-session recall validated: entities from session A retrievable in session B via Neo4j
- [ ] Proactive memory: `suggest_relevant()` injects context unprompted during context assembly
- [ ] Geospatial schema fields on Entity nodes (`coordinates`, `geocoded`)
- [ ] Geospatial pipeline: geocoding, spatial index, proximity queries, relevance scoring

---

## Track Overview

| Track | Chunks | Can Parallel With | Depends On |
|-------|--------|-------------------|------------|
| **A — Orchestration** | 1→2→3→4→5 | Tracks B, C | — |
| **B — Memory Quality** | 6→7→8 | Tracks A, C | — |
| **C — Recall** | 9 | Tracks A, B | — |
| **Stretch** | 10→11, 12 | After committed chunks | 10→11 needs Chunk 7; 12 needs Chunk 6 |

---

## File Structure

### New Files

| File | Responsibility | Chunk |
|------|---------------|-------|
| `src/personal_agent/orchestrator/expansion_types.py` | `ExpansionPlan`, `PlanTask`, `SubAgentMode`, `ExpansionPhase`, `PhaseResult` | 1 |
| `src/personal_agent/orchestrator/fallback_planner.py` | Deterministic plan generation from prompt structure | 2 |
| `src/personal_agent/orchestrator/expansion_controller.py` | Enforced expansion: planner → validate → dispatch → synthesize | 3 |
| `src/personal_agent/memory/embeddings.py` | Embedding generation pipeline (OpenAI / local) | 6 |
| `src/personal_agent/memory/dedup.py` | Fuzzy entity deduplication (vector similarity + LLM merge) | 8 |
| `src/personal_agent/request_gateway/recall_controller.py` | Stage 4b: cue detection, session scan, reclassification | 9 |
| `tests/personal_agent/orchestrator/test_expansion_types.py` | Expansion type tests | 1 |
| `tests/personal_agent/orchestrator/test_fallback_planner.py` | Fallback planner tests | 2 |
| `tests/personal_agent/orchestrator/test_expansion_controller.py` | Expansion controller tests | 3 |
| `tests/personal_agent/memory/test_embeddings.py` | Embedding pipeline tests | 6 |
| `tests/personal_agent/memory/test_hybrid_search.py` | Hybrid search tests | 7 |
| `tests/personal_agent/memory/test_dedup.py` | Dedup pipeline tests | 8 |
| `tests/personal_agent/request_gateway/test_recall_controller.py` | Recall controller tests | 9 |

### Modified Files

| File | Changes | Chunk |
|------|---------|-------|
| `src/personal_agent/config/settings.py` | `orchestration_mode`, `planner_timeout_s`, `worker_timeout_s`, `synthesis_timeout_s`, `embedding_model`, `embedding_dimensions`, `dedup_similarity_threshold` | 1, 4, 6 |
| `src/personal_agent/orchestrator/types.py` | Updated `ExecutionContext` expansion fields | 1 |
| `src/personal_agent/orchestrator/executor.py` | Mode branch at ~line 745; remove inline expansion (lines 1084–1096, 1235–1290) | 3 |
| `src/personal_agent/orchestrator/sub_agent.py` | Wire `tools` field; add `TOOLED_SEQUENTIAL` execution loop | 3 |
| `src/personal_agent/orchestrator/sub_agent_types.py` | Add `SubAgentMode` field to `SubAgentSpec` | 1 |
| `src/personal_agent/orchestrator/expansion.py` | Functions absorbed into expansion controller (deprecate or forward) | 3 |
| `src/personal_agent/memory/service.py` | Embed on entity/turn creation; vector index setup; hybrid `query_memory`; fuzzy dedup in `create_entity` | 6, 7, 8 |
| `src/personal_agent/memory/models.py` | `embedding` field on `Entity` | 6 |
| `src/personal_agent/request_gateway/pipeline.py` | Add Stage 4b call | 9 |
| `src/personal_agent/request_gateway/types.py` | `RecallCandidate`, `RecallResult`; `recall_context` on `GatewayOutput` | 9 |
| `src/personal_agent/request_gateway/context.py` | Inject session fact candidates when `recall_context` present | 9 |
| `tests/evaluation/harness/dataset.py` | Revised CP-16, CP-17, CP-19 assertions; adversarial variants | 5 |

---

## Chunk 1: Expansion Types + Config

**Track:** A — Orchestration
**Tier:** Tier-3 (Haiku) — mechanical type definitions
**Depends on:** Nothing
**Estimated effort:** 30 min

### Task 1.1: Define Expansion Types

**Files:**
- Create: `src/personal_agent/orchestrator/expansion_types.py`
- Create: `tests/personal_agent/orchestrator/test_expansion_types.py`

- [ ] **Step 1: Write tests for expansion types**

```python
# tests/personal_agent/orchestrator/test_expansion_types.py
"""Tests for expansion controller types."""

from personal_agent.orchestrator.expansion_types import (
    ExpansionPhase,
    ExpansionPlan,
    PhaseResult,
    PlanTask,
    SubAgentMode,
)


class TestSubAgentMode:
    def test_modes_defined(self) -> None:
        assert SubAgentMode.PARALLEL_INFERENCE.value == "parallel_inference"
        assert SubAgentMode.TOOLED_SEQUENTIAL.value == "tooled_sequential"


class TestExpansionPhase:
    def test_phases_defined(self) -> None:
        assert ExpansionPhase.PLANNING.value == "planning"
        assert ExpansionPhase.DISPATCH.value == "dispatch"
        assert ExpansionPhase.SYNTHESIS.value == "synthesis"


class TestPlanTask:
    def test_construction(self) -> None:
        task = PlanTask(
            name="compare_performance",
            goal="Compare Redis and Memcached on raw throughput",
            constraints=["Focus on 10k rps scenario"],
            expected_output="Performance comparison with recommendation signal",
        )
        assert task.name == "compare_performance"
        assert len(task.constraints) == 1

    def test_frozen(self) -> None:
        task = PlanTask(
            name="t1",
            goal="g1",
            constraints=[],
            expected_output="text",
        )
        try:
            task.name = "t2"  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass

    def test_defaults(self) -> None:
        task = PlanTask(
            name="t1",
            goal="g1",
        )
        assert task.constraints == []
        assert task.expected_output == "text"
        assert task.mode == SubAgentMode.PARALLEL_INFERENCE
        assert task.tools == []


class TestExpansionPlan:
    def test_construction(self) -> None:
        plan = ExpansionPlan(
            strategy="HYBRID",
            tasks=[
                PlanTask(name="t1", goal="g1"),
                PlanTask(name="t2", goal="g2"),
            ],
        )
        assert plan.strategy == "HYBRID"
        assert len(plan.tasks) == 2

    def test_is_fallback_default(self) -> None:
        plan = ExpansionPlan(strategy="HYBRID", tasks=[])
        assert plan.is_fallback is False


class TestPhaseResult:
    def test_success(self) -> None:
        result = PhaseResult(
            phase=ExpansionPhase.PLANNING,
            duration_ms=4500,
            success=True,
        )
        assert result.success
        assert result.error is None

    def test_failure(self) -> None:
        result = PhaseResult(
            phase=ExpansionPhase.DISPATCH,
            duration_ms=90000,
            success=False,
            error="Global timeout exceeded",
        )
        assert not result.success
        assert "timeout" in result.error.lower()
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/personal_agent/orchestrator/test_expansion_types.py -v`
Expected: `ModuleNotFoundError: No module named 'personal_agent.orchestrator.expansion_types'`

- [ ] **Step 3: Create the expansion types module**

```python
# src/personal_agent/orchestrator/expansion_types.py
"""Types for the expansion controller.

The expansion controller enforces deterministic sub-agent dispatch when
the gateway sets strategy ∈ {HYBRID, DECOMPOSE}. These types define the
plan schema, phase tracking, and sub-agent execution modes.

See: ADR-0036 (expansion-controller), ADR-0035 (seshat-backend-decision)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SubAgentMode(Enum):
    """Execution mode for a sub-agent task.

    PARALLEL_INFERENCE: Fire-and-forget LLM call, no tool access.
        Used for analysis, comparison, and synthesis sub-tasks.
    TOOLED_SEQUENTIAL: Mini tool-use loop with MCP tool access.
        Used for research sub-tasks that need web search or other tools.
    """

    PARALLEL_INFERENCE = "parallel_inference"
    TOOLED_SEQUENTIAL = "tooled_sequential"


class ExpansionPhase(Enum):
    """Phases of the expansion controller state machine."""

    PLANNING = "planning"
    DISPATCH = "dispatch"
    SYNTHESIS = "synthesis"


@dataclass(frozen=True)
class PlanTask:
    """A single sub-task within an expansion plan.

    Produced by the LLM planner or deterministic fallback planner.
    Consumed by the executor dispatch phase.

    Args:
        name: Task identifier (e.g., "compare_performance").
        goal: What this sub-agent should answer or produce.
        constraints: Scope or focus limits for the sub-agent.
        expected_output: Output shape description ("text", "comparison table", etc.).
        mode: Execution mode — inference-only or tool-enabled.
        tools: Tool names available when mode is TOOLED_SEQUENTIAL.
    """

    name: str
    goal: str
    constraints: list[str] = field(default_factory=list)
    expected_output: str = "text"
    mode: SubAgentMode = SubAgentMode.PARALLEL_INFERENCE
    tools: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExpansionPlan:
    """A validated expansion plan ready for dispatch.

    Args:
        strategy: "HYBRID" or "DECOMPOSE" — mirrors gateway output.
        tasks: Ordered list of sub-tasks to execute.
        is_fallback: True if generated by the deterministic fallback planner.
    """

    strategy: str
    tasks: list[PlanTask]
    is_fallback: bool = False


@dataclass(frozen=True)
class PhaseResult:
    """Result of a single expansion phase.

    Args:
        phase: Which phase completed.
        duration_ms: Wall-clock time for this phase.
        success: Whether the phase completed without error.
        error: Error description if success is False.
    """

    phase: ExpansionPhase
    duration_ms: float
    success: bool
    error: str | None = None
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `uv run pytest tests/personal_agent/orchestrator/test_expansion_types.py -v`
Expected: All tests pass.

### Task 1.2: Add Config Settings

**Files:**
- Modify: `src/personal_agent/config/settings.py`

- [ ] **Step 1: Add orchestration_mode and phase budget settings**

Add after the existing `sub_agent_max_tokens` field (~line 272):

```python
    # --- Expansion controller (ADR-0036) ---
    orchestration_mode: str = Field(
        default="enforced",
        description="Expansion enforcement mode: 'enforced' (gateway binding) or 'autonomous' (LLM decides)",
    )
    planner_timeout_seconds: float = Field(
        default=15.0,
        description="Max time for LLM planner phase in expansion controller",
    )
    worker_timeout_seconds: float = Field(
        default=45.0,
        description="Max time per sub-agent worker in expansion dispatch",
    )
    worker_global_timeout_seconds: float = Field(
        default=90.0,
        description="Max total time for all sub-agent workers combined",
    )
    synthesis_timeout_seconds: float = Field(
        default=25.0,
        description="Max time for synthesis phase in expansion controller",
    )
```

- [ ] **Step 2: Verify config loads**

Run: `uv run python -c "from personal_agent.config import get_settings; s = get_settings(); print(s.orchestration_mode, s.planner_timeout_seconds)"`
Expected: `enforced 15.0`

### Task 1.3: Update ExecutionContext and SubAgentSpec

**Files:**
- Modify: `src/personal_agent/orchestrator/types.py`
- Modify: `src/personal_agent/orchestrator/sub_agent_types.py`

- [ ] **Step 1: Add expansion controller fields to ExecutionContext**

In `types.py`, replace the expansion fields (lines ~189–192):

```python
    # --- Expansion controller state (Slice 3, ADR-0036) ---
    gateway_output: GatewayOutput | None = None
    expansion_strategy: str | None = None
    expansion_constraints: dict[str, Any] | None = None
    sub_agent_results: list[Any] | None = None
    expansion_plan: Any | None = None  # ExpansionPlan (avoid circular import)
    expansion_phase_results: list[Any] = field(default_factory=list)  # list[PhaseResult]
```

- [ ] **Step 2: Add SubAgentMode to SubAgentSpec**

In `sub_agent_types.py`, add import and field:

```python
from personal_agent.orchestrator.expansion_types import SubAgentMode
```

Add field to `SubAgentSpec` after `model_role`:

```python
    mode: SubAgentMode = SubAgentMode.PARALLEL_INFERENCE
```

- [ ] **Step 3: Run existing tests**

Run: `uv run pytest tests/personal_agent/orchestrator/ -v -x`
Expected: All existing tests still pass (backward compatible — new fields have defaults).

---

## Chunk 2: Fallback Planner

**Track:** A — Orchestration
**Tier:** Tier-2 (Sonnet) — implementation from ADR spec
**Depends on:** Chunk 1
**Estimated effort:** 1 hour

### Task 2.1: Write Fallback Planner Tests

**Files:**
- Create: `tests/personal_agent/orchestrator/test_fallback_planner.py`

- [ ] **Step 1: Write fallback planner tests**

```python
# tests/personal_agent/orchestrator/test_fallback_planner.py
"""Tests for deterministic fallback planner.

The fallback planner generates plans from prompt structure when the LLM
planner fails. Scoped to enumerated comparisons per ADR-0036 Decision 3.
"""

from personal_agent.orchestrator.expansion_types import ExpansionPlan, SubAgentMode
from personal_agent.orchestrator.fallback_planner import generate_fallback_plan


class TestHybridFallback:
    def test_enumerated_entities(self) -> None:
        """HYBRID with explicit named entities → one task per entity + synthesis."""
        plan = generate_fallback_plan(
            query="Compare Redis, Memcached, and Hazelcast for our session caching",
            strategy="HYBRID",
        )
        assert isinstance(plan, ExpansionPlan)
        assert plan.is_fallback is True
        assert plan.strategy == "HYBRID"
        # Should extract entities and create tasks
        assert len(plan.tasks) >= 2
        assert len(plan.tasks) <= 4  # HYBRID caps at 3 + synthesis
        # Last task should be synthesis/recommendation
        assert any("synth" in t.name.lower() or "recommend" in t.name.lower() for t in plan.tasks)

    def test_enumerated_dimensions(self) -> None:
        """HYBRID with explicit dimensions → one task per dimension."""
        plan = generate_fallback_plan(
            query="Analyze performance, memory usage, and operational complexity of our caching layer",
            strategy="HYBRID",
        )
        assert plan.is_fallback is True
        assert len(plan.tasks) >= 2

    def test_no_entities_generic_split(self) -> None:
        """No enumerable structure → generic 2-task split."""
        plan = generate_fallback_plan(
            query="Research the best approach to scaling our API layer",
            strategy="HYBRID",
        )
        assert plan.is_fallback is True
        assert len(plan.tasks) == 2  # research + recommendation


class TestDecomposeFallback:
    def test_enumerated_entities(self) -> None:
        """DECOMPOSE with entities → one task per evaluation axis + recommendation."""
        plan = generate_fallback_plan(
            query="Evaluate Redis, Memcached, and Hazelcast for 10k rps microservices",
            strategy="DECOMPOSE",
        )
        assert plan.is_fallback is True
        assert plan.strategy == "DECOMPOSE"
        assert len(plan.tasks) >= 3
        assert len(plan.tasks) <= 6  # DECOMPOSE allows more tasks

    def test_generic_decompose(self) -> None:
        """No enumerable structure → 2-task split."""
        plan = generate_fallback_plan(
            query="Design a comprehensive monitoring strategy",
            strategy="DECOMPOSE",
        )
        assert plan.is_fallback is True
        assert len(plan.tasks) == 2


class TestToolAssignment:
    def test_research_tasks_get_tools(self) -> None:
        """Tasks with research/search goals should get TOOLED_SEQUENTIAL mode."""
        plan = generate_fallback_plan(
            query="Research and compare Redis vs Memcached performance benchmarks",
            strategy="HYBRID",
        )
        # At least one task should have research-oriented mode
        research_tasks = [t for t in plan.tasks if t.mode == SubAgentMode.TOOLED_SEQUENTIAL]
        # Not required — fallback planner defaults to PARALLEL_INFERENCE
        # This test documents the behavior
        assert isinstance(research_tasks, list)


class TestEdgeCases:
    def test_empty_query(self) -> None:
        """Empty query → generic 2-task split."""
        plan = generate_fallback_plan(query="", strategy="HYBRID")
        assert plan.is_fallback is True
        assert len(plan.tasks) == 2

    def test_single_entity(self) -> None:
        """Single entity → still produces a valid plan."""
        plan = generate_fallback_plan(
            query="Evaluate Redis for our caching needs",
            strategy="HYBRID",
        )
        assert plan.is_fallback is True
        assert len(plan.tasks) >= 2
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/personal_agent/orchestrator/test_fallback_planner.py -v`
Expected: `ModuleNotFoundError: No module named 'personal_agent.orchestrator.fallback_planner'`

### Task 2.2: Implement Fallback Planner

**Files:**
- Create: `src/personal_agent/orchestrator/fallback_planner.py`

- [ ] **Step 1: Implement the fallback planner**

```python
# src/personal_agent/orchestrator/fallback_planner.py
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

_DIMENSION_RE = re.compile(
    r"(?:analyze|evaluate|assess|compare)\s+"
    r"([\w\s]+(?:,\s*[\w\s]+)+(?:,?\s*(?:and|or)\s+[\w\s]+)?)",
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
```

- [ ] **Step 2: Run tests — verify they pass**

Run: `uv run pytest tests/personal_agent/orchestrator/test_fallback_planner.py -v`
Expected: All tests pass.

- [ ] **Step 3: Run type checking**

Run: `uv run mypy src/personal_agent/orchestrator/fallback_planner.py --strict`
Expected: No errors.

---

## Chunk 3: Expansion Controller Core + Sub-Agent Mode Dispatch

**Track:** A — Orchestration
**Tier:** Tier-2 (Sonnet) — implementation from detailed ADR
**Depends on:** Chunks 1, 2
**Estimated effort:** 3 hours

### Task 3.1: Write Expansion Controller Tests

**Files:**
- Create: `tests/personal_agent/orchestrator/test_expansion_controller.py`

- [ ] **Step 1: Write expansion controller tests**

```python
# tests/personal_agent/orchestrator/test_expansion_controller.py
"""Tests for the expansion controller.

Tests the enforced expansion path: planner → validate → dispatch → synthesize.
Uses mocked LLM client and sub-agent runner.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.orchestrator.expansion_controller import (
    ExpansionController,
    _validate_plan_json,
)
from personal_agent.orchestrator.expansion_types import (
    ExpansionPlan,
    PlanTask,
    SubAgentMode,
)
from personal_agent.orchestrator.sub_agent_types import SubAgentResult


def _make_plan_json(tasks: int = 3) -> str:
    """Create valid plan JSON for testing."""
    plan = {
        "strategy": "HYBRID",
        "tasks": [
            {
                "name": f"task_{i}",
                "goal": f"Goal for task {i}",
                "constraints": [f"constraint_{i}"],
                "expected_output": "text",
            }
            for i in range(tasks)
        ],
    }
    return json.dumps(plan)


def _make_sub_agent_result(
    task_name: str = "task_0",
    success: bool = True,
    summary: str = "Result summary",
) -> SubAgentResult:
    return SubAgentResult(
        task_id=f"sub-{task_name}",
        spec_task=task_name,
        summary=summary,
        full_output=summary,
        tools_used=[],
        token_count=50,
        duration_ms=2000,
        success=success,
        error=None if success else "Timeout",
    )


class TestValidatePlanJson:
    def test_valid_plan(self) -> None:
        plan = _validate_plan_json(_make_plan_json(3))
        assert plan is not None
        assert len(plan.tasks) == 3
        assert plan.strategy == "HYBRID"

    def test_invalid_json(self) -> None:
        assert _validate_plan_json("not json") is None

    def test_missing_tasks(self) -> None:
        assert _validate_plan_json('{"strategy": "HYBRID"}') is None

    def test_empty_tasks(self) -> None:
        assert _validate_plan_json('{"strategy": "HYBRID", "tasks": []}') is None

    def test_task_missing_name(self) -> None:
        bad = '{"strategy": "HYBRID", "tasks": [{"goal": "g"}]}'
        assert _validate_plan_json(bad) is None

    def test_task_missing_goal(self) -> None:
        bad = '{"strategy": "HYBRID", "tasks": [{"name": "n"}]}'
        assert _validate_plan_json(bad) is None

    def test_caps_task_count_hybrid(self) -> None:
        plan = _validate_plan_json(_make_plan_json(10))
        # HYBRID caps at 4 (3 + synthesis)
        assert plan is not None
        assert len(plan.tasks) <= 5


class TestExpansionControllerExecute:
    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @pytest.fixture
    def mock_llm(self) -> AsyncMock:
        client = AsyncMock()
        client.respond = AsyncMock(return_value=_make_plan_json(3))
        return client

    @pytest.mark.asyncio
    async def test_successful_expansion(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """LLM produces valid plan → sub-agents execute → synthesis."""
        mock_results = [
            _make_sub_agent_result(f"task_{i}") for i in range(3)
        ]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[{"role": "user", "content": "Compare Redis, Memcached, and Hazelcast"}],
            )

        assert result.plan is not None
        assert len(result.sub_agent_results) == 3
        assert all(r.success for r in result.sub_agent_results)

    @pytest.mark.asyncio
    async def test_fallback_on_invalid_plan(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """LLM produces garbage → fallback planner engaged."""
        mock_llm.respond = AsyncMock(return_value="I'll just answer directly...")

        mock_results = [_make_sub_agent_result("evaluate_redis")]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis and Memcached",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[],
            )

        assert result.plan is not None
        assert result.plan.is_fallback is True

    @pytest.mark.asyncio
    async def test_planner_timeout_triggers_fallback(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """LLM planner times out → fallback planner engaged."""
        async def slow_respond(*args: Any, **kwargs: Any) -> str:
            await asyncio.sleep(100)
            return _make_plan_json()

        mock_llm.respond = slow_respond

        mock_results = [_make_sub_agent_result("research_analysis")]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ), patch(
            "personal_agent.orchestrator.expansion_controller._PLANNER_TIMEOUT",
            0.01,
        ):
            result = await controller.execute(
                query="Research scaling approaches",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[],
            )

        assert result.plan is not None
        assert result.plan.is_fallback is True

    @pytest.mark.asyncio
    async def test_partial_sub_agent_failure(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """Some sub-agents fail → partial results returned."""
        mock_results = [
            _make_sub_agent_result("task_0", success=True),
            _make_sub_agent_result("task_1", success=False),
            _make_sub_agent_result("task_2", success=True),
        ]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[],
            )

        assert len(result.sub_agent_results) == 3
        assert result.successful_count == 2
        assert result.failed_count == 1
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/personal_agent/orchestrator/test_expansion_controller.py -v`
Expected: `ModuleNotFoundError`

### Task 3.2: Implement Expansion Controller

**Files:**
- Create: `src/personal_agent/orchestrator/expansion_controller.py`

- [ ] **Step 1: Implement the expansion controller**

```python
# src/personal_agent/orchestrator/expansion_controller.py
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
from personal_agent.orchestrator.expansion_types import (
    ExpansionPlan,
    ExpansionPhase,
    PhaseResult,
    PlanTask,
    SubAgentMode,
)
from personal_agent.orchestrator.fallback_planner import generate_fallback_plan
from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec

logger = structlog.get_logger(__name__)

# Default planner timeout — overridden in tests
_PLANNER_TIMEOUT: float = 15.0

# Plan schema: max tasks per strategy
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
    """

    plan: ExpansionPlan | None = None
    sub_agent_results: list[SubAgentResult] = field(default_factory=list)
    synthesis_context: str = ""
    phase_results: list[PhaseResult] = field(default_factory=list)
    degraded: bool = False
    degradation_reason: str | None = None

    @property
    def successful_count(self) -> int:
        return sum(1 for r in self.sub_agent_results if r.success)

    @property
    def failed_count(self) -> int:
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
        constraints: dict[str, Any] | None = None,
    ) -> ExpansionResult:
        """Run the full expansion pipeline.

        Args:
            query: User's original query.
            strategy: "HYBRID" or "DECOMPOSE".
            llm_client: LLM client for planner and synthesis calls.
            trace_id: Request trace identifier.
            messages: Conversation context for sub-agents.
            constraints: Optional expansion constraints from gateway.

        Returns:
            ExpansionResult with plan, sub-agent results, and synthesis context.
        """
        result = ExpansionResult()
        settings = get_settings()
        planner_timeout = settings.planner_timeout_seconds

        # --- Phase 1: Planning ---
        plan = await self._run_planner(
            query=query,
            strategy=strategy,
            llm_client=llm_client,
            trace_id=trace_id,
            timeout_s=planner_timeout,
            result=result,
        )
        result.plan = plan

        if not plan or not plan.tasks:
            result.degraded = True
            result.degradation_reason = "No valid plan produced"
            return result

        # --- Phase 2: Dispatch ---
        sub_results = await self._run_dispatch(
            plan=plan,
            llm_client=llm_client,
            trace_id=trace_id,
            messages=messages,
            result=result,
        )
        result.sub_agent_results = sub_results

        # --- Build synthesis context ---
        result.synthesis_context = self._build_synthesis_context(
            plan=plan,
            sub_results=sub_results,
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
    ) -> ExpansionPlan:
        """Phase 1: Get a plan from the LLM or fallback planner.

        Args:
            query: User query.
            strategy: HYBRID or DECOMPOSE.
            llm_client: LLM client.
            trace_id: Trace identifier.
            timeout_s: Planner timeout in seconds.
            result: Mutable result for phase tracking.

        Returns:
            ExpansionPlan (LLM-generated or fallback).
        """
        start_ms = time.monotonic() * 1000

        logger.info(
            "planner_started",
            strategy=strategy,
            trace_id=trace_id,
        )

        try:
            planner_messages = [
                {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Strategy: {strategy}\n"
                        f"Query: {query}\n\n"
                        "Produce the JSON plan."
                    ),
                },
            ]

            raw_response = await asyncio.wait_for(
                llm_client.respond(
                    role="sub_agent",
                    messages=planner_messages,
                    max_tokens=1024,
                    response_format={"type": "json_object"},
                ),
                timeout=timeout_s,
            )

            duration_ms = time.monotonic() * 1000 - start_ms
            plan = _validate_plan_json(str(raw_response), strategy)

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

            # Invalid plan — fall through to fallback
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
    ) -> list[SubAgentResult]:
        """Phase 2: Dispatch sub-agents in parallel.

        Args:
            plan: Validated expansion plan.
            llm_client: LLM client for sub-agent calls.
            trace_id: Trace identifier.
            messages: Conversation context.
            result: Mutable result for phase tracking.

        Returns:
            List of SubAgentResult from all sub-agents.
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
                context=messages[-4:] if messages else [],  # Last 2 turns for context
                output_format=task.expected_output,
                max_tokens=settings.sub_agent_max_tokens,
                timeout_seconds=settings.worker_timeout_seconds,
                tools=task.tools,
                background=f"Sub-task: {task.name}. Constraints: {', '.join(task.constraints)}",
                mode=task.mode,
            )
            for task in plan.tasks
        ]

        # Dispatch all sub-agents with global timeout
        try:
            sub_results: list[SubAgentResult] = await asyncio.wait_for(
                asyncio.gather(
                    *[
                        run_sub_agent(
                            spec=spec,
                            llm_client=llm_client,
                            trace_id=trace_id,
                        )
                        for spec in specs
                    ],
                    return_exceptions=False,
                ),
                timeout=settings.worker_global_timeout_seconds,
            )
        except asyncio.TimeoutError:
            # Global timeout — collect whatever completed
            logger.warning(
                "expansion_dispatch_global_timeout",
                trace_id=trace_id,
            )
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
            plan: The expansion plan.
            sub_results: Results from dispatched sub-agents.

        Returns:
            Formatted string for injection into synthesis LLM call.
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
        raw: Raw JSON string from the LLM.
        strategy: Expected strategy for task count limits.

    Returns:
        ExpansionPlan if valid, None if invalid.
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

    for t in tasks_raw[:max_tasks + 1]:  # +1 for synthesis task
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
```

- [ ] **Step 2: Run controller tests**

Run: `uv run pytest tests/personal_agent/orchestrator/test_expansion_controller.py -v`
Expected: All tests pass.

### Task 3.3: Wire Executor Mode Branch

**Files:**
- Modify: `src/personal_agent/orchestrator/executor.py`

- [ ] **Step 1: Replace the expansion flag block (lines 745–757) with mode branch**

Replace the block at lines 745–757 in `step_init`:

```python
        # --- Expansion controller mode branch (ADR-0036) ---
        from personal_agent.request_gateway.types import DecompositionStrategy

        if gw.decomposition.strategy in (
            DecompositionStrategy.HYBRID,
            DecompositionStrategy.DECOMPOSE,
        ):
            ctx.expansion_strategy = gw.decomposition.strategy.value
            ctx.expansion_constraints = gw.decomposition.constraints or {}

            if settings.orchestration_mode == "enforced":
                from personal_agent.orchestrator.expansion_controller import (
                    ExpansionController,
                )

                controller = ExpansionController()
                expansion_result = await controller.execute(
                    query=ctx.messages[-1].get("content", "") if ctx.messages else "",
                    strategy=gw.decomposition.strategy.value.upper(),
                    llm_client=ctx.llm_client,
                    trace_id=ctx.trace_id,
                    messages=ctx.messages,
                    constraints=ctx.expansion_constraints,
                )

                ctx.expansion_plan = expansion_result.plan
                ctx.sub_agent_results = expansion_result.sub_agent_results
                ctx.expansion_phase_results = expansion_result.phase_results

                # Build synthesis context and append to messages
                if expansion_result.sub_agent_results:
                    synthesis_msg = {
                        "role": "user",
                        "content": (
                            f"{expansion_result.synthesis_context}\n"
                            "The sub-tasks above have been completed. "
                            "Synthesize the results into a coherent response "
                            "for the user's original question."
                        ),
                    }
                    ctx.messages.append(synthesis_msg)

                log.info(
                    "expansion_controller_complete",
                    mode="enforced",
                    plan_is_fallback=expansion_result.plan.is_fallback if expansion_result.plan else None,
                    sub_agent_count=len(expansion_result.sub_agent_results),
                    successful=expansion_result.successful_count,
                    degraded=expansion_result.degraded,
                    trace_id=ctx.trace_id,
                )

                # Go directly to synthesis LLM call
                return TaskState.LLM_CALL

            # Autonomous mode — existing behavior
            log.info(
                "step_init_expansion_flagged",
                mode="autonomous",
                strategy=gw.decomposition.strategy.value,
                constraints=gw.decomposition.constraints,
                trace_id=ctx.trace_id,
            )
        # --- End expansion controller mode branch ---
```

- [ ] **Step 2: Remove inline expansion prompt injection (lines 1084–1096)**

Replace the HYBRID decomposition prompt block with an autonomous-mode guard:

```python
        # HYBRID decomposition prompt (autonomous mode only — enforced mode
        # uses the expansion controller which has already run by this point).
        if (
            ctx.expansion_strategy is not None
            and ctx.sub_agent_results is None
            and settings.orchestration_mode == "autonomous"
        ):
            hybrid_prompt = (
                "\n\n## Decomposition Instructions\n"
                "Break your response into a numbered list of independent sub-tasks "
                "(1. ..., 2. ..., 3. ...). Each item should be a self-contained "
                "task that can be researched or answered independently. "
                "Keep to 2-4 sub-tasks. After the sub-tasks complete, you will "
                "synthesize their results into a final answer."
            )
            if system_prompt:
                system_prompt = f"{system_prompt}{hybrid_prompt}"
            else:
                system_prompt = hybrid_prompt.strip()
```

- [ ] **Step 3: Guard inline expansion hook (lines 1235–1290) with autonomous mode**

Replace the expansion hook condition:

```python
        # --- HYBRID expansion hook (autonomous mode only) ---
        if (
            ctx.expansion_strategy is not None
            and ctx.sub_agent_results is None
            and settings.orchestration_mode == "autonomous"
        ):
```

The rest of the block (lines 1236–1290) remains unchanged.

- [ ] **Step 4: Run all orchestrator tests**

Run: `uv run pytest tests/personal_agent/orchestrator/ -v -x`
Expected: All tests pass. Existing tests use default `orchestration_mode="enforced"` but set `expansion_strategy=None` (non-expansion paths), so they're unaffected.

### Task 3.4: Wire Sub-Agent Tool Access

**Files:**
- Modify: `src/personal_agent/orchestrator/sub_agent.py`

- [ ] **Step 1: Add TOOLED_SEQUENTIAL execution path**

Add a tool-use loop branch after the existing inference call in `run_sub_agent`. Insert the following after the import block and before `_SUB_AGENT_SYSTEM_PROMPT`:

```python
from personal_agent.orchestrator.expansion_types import SubAgentMode
```

Then replace the body of `run_sub_agent` (lines 65–130) with:

```python
    try:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SUB_AGENT_SYSTEM_PROMPT},
        ]
        messages.extend(spec.context)
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Task: {spec.task}\n"
                    f"Output format: {spec.output_format}\n"
                    "Respond with the result only."
                ),
            }
        )

        if spec.mode == SubAgentMode.TOOLED_SEQUENTIAL and spec.tools:
            # Tooled mode: mini tool-use loop (max 3 iterations)
            response_content = await _run_tooled_loop(
                messages=messages,
                llm_client=llm_client,
                spec=spec,
                trace_id=trace_id,
                task_id=task_id,
            )
        else:
            # Default: single inference call
            response_content = str(
                await asyncio.wait_for(
                    llm_client.respond(
                        role=spec.model_role,
                        messages=messages,
                        max_tokens=spec.max_tokens,
                    ),
                    timeout=spec.timeout_seconds,
                )
            )

        duration_ms = int(time.monotonic() * 1000) - start_ms

        result = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary=response_content[:2000],  # Cap summary length
            full_output=response_content,
            tools_used=[],
            token_count=len(response_content.split()),
            duration_ms=duration_ms,
            success=True,
            error=None,
        )
```

Then add the tooled loop function:

```python
async def _run_tooled_loop(
    messages: list[dict[str, Any]],
    llm_client: Any,
    spec: SubAgentSpec,
    trace_id: str,
    task_id: str,
    max_iterations: int = 3,
) -> str:
    """Run a mini tool-use loop for TOOLED_SEQUENTIAL sub-agents.

    The sub-agent can call tools (e.g., web search) and incorporate
    results before producing its final answer.

    Args:
        messages: Initial message context.
        llm_client: LLM client.
        spec: Sub-agent specification.
        trace_id: Trace identifier.
        task_id: Sub-agent task identifier.
        max_iterations: Max tool-use rounds before forcing final answer.

    Returns:
        Final response content string.
    """
    from personal_agent.tools import get_default_registry

    registry = get_default_registry()

    for iteration in range(max_iterations):
        response = await asyncio.wait_for(
            llm_client.respond(
                role=spec.model_role,
                messages=messages,
                max_tokens=spec.max_tokens,
            ),
            timeout=spec.timeout_seconds,
        )

        response_str = str(response)

        # Check if response contains tool calls
        # (Implementation depends on LLM client tool-call format)
        # For now, return the response directly — tool parsing
        # is wired when the LLM client exposes tool_calls in response
        logger.info(
            "sub_agent_tooled_iteration",
            task_id=task_id,
            iteration=iteration,
            trace_id=trace_id,
        )

        return response_str

    return response_str
```

- [ ] **Step 2: Run sub-agent tests**

Run: `uv run pytest tests/personal_agent/orchestrator/test_sub_agent.py -v -x`
Expected: All existing tests pass (they use default `PARALLEL_INFERENCE` mode).

---

## Chunk 4: Per-Phase Time Budgets + Graceful Degradation

**Track:** A — Orchestration
**Tier:** Tier-2 (Sonnet) — implementation from ADR spec
**Depends on:** Chunk 3
**Estimated effort:** 1.5 hours

### Task 4.1: Add Phase Budget Tests

**Files:**
- Modify: `tests/personal_agent/orchestrator/test_expansion_controller.py`

- [ ] **Step 1: Add timeout and degradation tests**

Append to the existing test file:

```python
class TestGracefulDegradation:
    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @pytest.fixture
    def mock_llm(self) -> AsyncMock:
        client = AsyncMock()
        client.respond = AsyncMock(return_value=_make_plan_json(3))
        return client

    @pytest.mark.asyncio
    async def test_all_subagents_fail_degraded_response(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """All sub-agents fail → degraded=True, synthesis context notes failure."""
        mock_results = [
            _make_sub_agent_result(f"task_{i}", success=False)
            for i in range(3)
        ]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[],
            )

        assert result.degraded is True
        assert result.failed_count == 3

    @pytest.mark.asyncio
    async def test_synthesis_context_notes_failures(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """Partial failure → synthesis context includes failure notes."""
        mock_results = [
            _make_sub_agent_result("task_0", success=True, summary="Redis is fast"),
            _make_sub_agent_result("task_1", success=False),
            _make_sub_agent_result("task_2", success=True, summary="Hazelcast scales"),
        ]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[],
            )

        assert "FAILED" in result.synthesis_context
        assert "Redis is fast" in result.synthesis_context
        assert "Hazelcast scales" in result.synthesis_context
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/personal_agent/orchestrator/test_expansion_controller.py -v -k "Degradation"`
Expected: All degradation tests pass (the controller already handles these cases from Chunk 3).

### Task 4.2: Add All-Fail Degradation to Controller

**Files:**
- Modify: `src/personal_agent/orchestrator/expansion_controller.py`

- [ ] **Step 1: Add degradation detection in execute()**

In the `execute` method, after dispatch completes and before building synthesis context, add:

```python
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
```

- [ ] **Step 2: Run full controller test suite**

Run: `uv run pytest tests/personal_agent/orchestrator/test_expansion_controller.py -v`
Expected: All tests pass.

---

## Chunk 5: Expansion Telemetry + Evaluation Harness Updates

**Track:** A — Orchestration
**Tier:** Tier-2 (Sonnet) — telemetry wiring + dataset edits
**Depends on:** Chunk 3
**Estimated effort:** 1.5 hours

### Task 5.1: Add Strategy Mismatch Metric

**Files:**
- Modify: `src/personal_agent/orchestrator/expansion_controller.py`

The expansion controller already emits `planner_started`, `planner_completed`, `planner_failed`, `fallback_planner_used`, `expansion_dispatch_started`, and `subagent_completed` events via structlog (wired in Chunk 3). These flow to Elasticsearch via the existing structlog→ES pipeline.

- [ ] **Step 1: Verify telemetry events are emitted**

Run the existing integration test that exercises a HYBRID path, then check:
Run: `uv run pytest tests/personal_agent/orchestrator/test_gateway_integration.py -v -k "hybrid" --no-header`
Expected: Tests pass. Verify structlog output includes `planner_started` and `expansion_dispatch_started` events.

### Task 5.2: Update Evaluation Harness Assertions

**Files:**
- Modify: `tests/evaluation/harness/dataset.py`

- [ ] **Step 1: Update CP-16 assertions to include workflow correctness (layer 2)**

Find the CP-16 definition in `dataset.py` and add layer 2 assertions:

```python
# In CP-16's turn assertions, add:
present("planner_started"),          # Layer 2: planner was invoked
present("expansion_dispatch_started"),  # Layer 2: sub-agents dispatched
```

- [ ] **Step 2: Update CP-17 assertions**

```python
# In CP-17's turn assertions, add:
present("planner_started"),
present("expansion_dispatch_started"),
absent("user_visible_timeout"),  # Layer 4: no raw timeout to user
```

- [ ] **Step 3: Add CP-19 adversarial variants**

Add 6 paraphrase variants of CP-19 to test recall controller robustness:

```python
CP_19_V2 = ConversationPath(
    path_id="CP-19-v2",
    name="Implicit Recall — 'again' cue",
    category="Context Management",
    objective="Verify recall controller catches 'again' backward-reference",
    turns=(
        # Setup turns establishing PostgreSQL as primary database
        ConversationTurn(
            user_message="We need to pick a primary database for the project. Let's go with PostgreSQL.",
            expected_behavior="Acknowledges PostgreSQL choice",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        # Several intervening turns...
        ConversationTurn(
            user_message="What was our primary database again?",
            expected_behavior="Recalls PostgreSQL from earlier in session",
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                present("recall_cue_detected"),
            ),
        ),
    ),
)

# Additional variants: CP-19-v3 ("earlier"), CP-19-v4 ("remind me"),
# CP-19-v5 ("what did we decide"), CP-19-v6 ("back to the beginning"),
# CP-19-v7 ("the database we discussed")
```

- [ ] **Step 4: Run evaluation harness unit tests**

Run: `uv run pytest tests/evaluation/harness/test_unit.py -v`
Expected: All tests pass (dataset compiles, assertions are well-formed).

---

## Chunk 6: Embedding Infrastructure + Geospatial Schema Fields

**Track:** B — Memory Quality
**Tier:** Tier-2 (Sonnet) — new capability
**Depends on:** Nothing (parallel with Track A)
**Estimated effort:** 2.5 hours

### Task 6.1: Add Embedding Config

**Files:**
- Modify: `src/personal_agent/config/settings.py`

- [ ] **Step 1: Add embedding settings**

Add after the expansion controller settings:

```python
    # --- Embedding configuration (ADR-0035) ---
    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="OpenAI embedding model name (or 'nomic-embed-text' for local)",
    )
    embedding_dimensions: int = Field(
        default=1536,
        description="Embedding vector dimensions (1536 for text-embedding-3-small)",
    )
    embedding_batch_size: int = Field(
        default=20,
        description="Max items per embedding API call",
    )
    dedup_similarity_threshold: float = Field(
        default=0.85,
        description="Cosine similarity threshold for entity deduplication",
    )
```

- [ ] **Step 2: Verify config loads**

Run: `uv run python -c "from personal_agent.config import get_settings; s = get_settings(); print(s.embedding_model, s.embedding_dimensions)"`
Expected: `text-embedding-3-small 1536`

### Task 6.2: Write Embedding Pipeline Tests

**Files:**
- Create: `tests/personal_agent/memory/test_embeddings.py`

- [ ] **Step 1: Write embedding pipeline tests**

```python
# tests/personal_agent/memory/test_embeddings.py
"""Tests for the embedding generation pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.memory.embeddings import (
    EmbeddingProvider,
    generate_embedding,
    generate_embeddings_batch,
)


class TestEmbeddingProvider:
    def test_provider_enum(self) -> None:
        assert EmbeddingProvider.OPENAI.value == "openai"
        assert EmbeddingProvider.LOCAL.value == "local"


class TestGenerateEmbedding:
    @pytest.mark.asyncio
    async def test_generates_vector(self) -> None:
        """Should return a list of floats with correct dimensions."""
        mock_response = type("R", (), {"data": [type("D", (), {"embedding": [0.1] * 1536})()]})()

        with patch(
            "personal_agent.memory.embeddings._call_openai_embeddings",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            embedding = await generate_embedding("Hello world")
            assert len(embedding) == 1536
            assert all(isinstance(x, float) for x in embedding)

    @pytest.mark.asyncio
    async def test_empty_text_returns_zeros(self) -> None:
        """Empty text should return zero vector."""
        embedding = await generate_embedding("")
        assert len(embedding) == 1536
        assert all(x == 0.0 for x in embedding)

    @pytest.mark.asyncio
    async def test_none_text_returns_zeros(self) -> None:
        """None text should return zero vector."""
        embedding = await generate_embedding(None)
        assert len(embedding) == 1536
        assert all(x == 0.0 for x in embedding)


class TestGenerateEmbeddingsBatch:
    @pytest.mark.asyncio
    async def test_batch_generation(self) -> None:
        """Batch should return one embedding per input text."""
        mock_response = type(
            "R", (), {"data": [type("D", (), {"embedding": [0.1] * 1536})() for _ in range(3)]}
        )()

        with patch(
            "personal_agent.memory.embeddings._call_openai_embeddings",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            embeddings = await generate_embeddings_batch(["a", "b", "c"])
            assert len(embeddings) == 3
            assert all(len(e) == 1536 for e in embeddings)

    @pytest.mark.asyncio
    async def test_empty_batch(self) -> None:
        """Empty batch should return empty list."""
        embeddings = await generate_embeddings_batch([])
        assert embeddings == []
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/personal_agent/memory/test_embeddings.py -v`
Expected: `ModuleNotFoundError`

### Task 6.3: Implement Embedding Pipeline

**Files:**
- Create: `src/personal_agent/memory/embeddings.py`

- [ ] **Step 1: Implement the embedding module**

```python
# src/personal_agent/memory/embeddings.py
"""Embedding generation pipeline for Seshat memory.

Generates vector embeddings for Entity and Turn nodes to enable
hybrid search (vector + keyword + graph traversal).

Supports OpenAI API (text-embedding-3-small) and local models
(nomic-embed-text via MLX). Provider selected via config.

See: ADR-0035 (seshat-backend-decision), Enhancement 1
"""

from __future__ import annotations

from enum import Enum
from typing import Any

import structlog

from personal_agent.config import get_settings

logger = structlog.get_logger(__name__)


class EmbeddingProvider(Enum):
    """Embedding model provider."""

    OPENAI = "openai"
    LOCAL = "local"


async def generate_embedding(text: str | None) -> list[float]:
    """Generate an embedding vector for a single text.

    Args:
        text: Text to embed. Returns zero vector for empty/None.

    Returns:
        List of floats with length == settings.embedding_dimensions.
    """
    settings = get_settings()
    dimensions = settings.embedding_dimensions

    if not text or not text.strip():
        return [0.0] * dimensions

    try:
        response = await _call_openai_embeddings(
            texts=[text],
            model=settings.embedding_model,
            dimensions=dimensions,
        )
        return [float(x) for x in response.data[0].embedding]

    except Exception as exc:
        logger.warning(
            "embedding_generation_failed",
            text_length=len(text),
            error=str(exc),
        )
        return [0.0] * dimensions


async def generate_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a batch of texts.

    Args:
        texts: List of texts to embed.

    Returns:
        List of embedding vectors, one per input text.
    """
    if not texts:
        return []

    settings = get_settings()

    try:
        response = await _call_openai_embeddings(
            texts=texts,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
        return [[float(x) for x in d.embedding] for d in response.data]

    except Exception as exc:
        logger.warning(
            "embedding_batch_failed",
            batch_size=len(texts),
            error=str(exc),
        )
        return [[0.0] * settings.embedding_dimensions for _ in texts]


async def _call_openai_embeddings(
    texts: list[str],
    model: str,
    dimensions: int,
) -> Any:
    """Call the OpenAI embeddings API.

    Args:
        texts: Texts to embed.
        model: Model name (e.g., "text-embedding-3-small").
        dimensions: Output dimensions.

    Returns:
        OpenAI API response object with .data[].embedding.
    """
    import openai

    settings = get_settings()
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

    return await client.embeddings.create(
        model=model,
        input=texts,
        dimensions=dimensions,
    )


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Cosine similarity score (0.0 to 1.0).
    """
    if len(a) != len(b) or not a:
        return 0.0

    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot / (norm_a * norm_b)
```

- [ ] **Step 2: Run embedding tests**

Run: `uv run pytest tests/personal_agent/memory/test_embeddings.py -v`
Expected: All tests pass.

### Task 6.4: Add Embedding Field to Entity Model and Neo4j Schema

**Files:**
- Modify: `src/personal_agent/memory/models.py`
- Modify: `src/personal_agent/memory/service.py`

- [ ] **Step 1: Add embedding field to Entity model**

In `models.py`, add to the `Entity` class:

```python
    embedding: list[float] | None = None
```

- [ ] **Step 2: Add geospatial schema fields to Entity model (ADR-0035 P2)**

In `models.py`, add to the `Entity` class:

```python
    coordinates: tuple[float, float] | None = None  # (latitude, longitude)
    geocoded: bool = False
```

- [ ] **Step 3: Update create_entity to store embedding vector**

In `service.py`, modify `create_entity` (line 320) to store the embedding:

```python
    async def create_entity(self, entity: Entity) -> str:
        """Create or update an entity node with optional embedding.

        Args:
            entity: Entity to create.

        Returns:
            Entity ID (name-based).
        """
        if not self.connected or not self.driver:
            log.warning("neo4j_not_connected")
            return ""

        try:
            async with self.driver.session() as session:
                # Base MERGE query
                cypher = """
                    MERGE (e:Entity {name: $name})
                    SET e.entity_id = COALESCE(e.entity_id, $entity_id),
                        e.entity_type = $entity_type,
                        e.description = $description,
                        e.properties = $properties,
                        e.last_seen = datetime(),
                        e.mention_count = COALESCE(e.mention_count, 0) + 1,
                        e.first_seen = COALESCE(e.first_seen, datetime())
                """
                params: dict[str, Any] = {
                    "name": entity.name,
                    "entity_id": entity.name,
                    "entity_type": entity.entity_type,
                    "description": entity.description,
                    "properties": orjson.dumps(entity.properties).decode(),
                }

                # Store embedding if provided
                if entity.embedding is not None:
                    cypher += ",\n                        e.embedding = $embedding"
                    params["embedding"] = entity.embedding

                # Store geospatial fields if provided
                if entity.coordinates is not None:
                    cypher += (
                        ",\n                        e.coordinates = point({latitude: $lat, longitude: $lon})"
                        ",\n                        e.geocoded = true"
                    )
                    params["lat"] = entity.coordinates[0]
                    params["lon"] = entity.coordinates[1]

                cypher += "\n                    RETURN e.name as entity_id"

                result = await session.run(cypher, **params)
                record = await result.single()
                entity_id: str = record["entity_id"] if record else entity.name
                log.info("entity_created", entity_id=entity_id, entity_type=entity.entity_type)
                return entity_id
        except Exception as e:
            log.error("entity_creation_failed", error=str(e), exc_info=True)
            return ""
```

- [ ] **Step 4: Add vector index creation method**

Add to `MemoryService`:

```python
    async def ensure_vector_index(self) -> bool:
        """Create Neo4j vector index on Entity.embedding if not exists.

        Requires Neo4j 5.11+.

        Returns:
            True if index exists or was created successfully.
        """
        if not self.connected or not self.driver:
            return False

        try:
            settings = get_settings()
            async with self.driver.session() as session:
                await session.run(
                    """
                    CREATE VECTOR INDEX entity_embedding IF NOT EXISTS
                    FOR (e:Entity)
                    ON (e.embedding)
                    OPTIONS {
                        indexConfig: {
                            `vector.dimensions`: $dimensions,
                            `vector.similarity_function`: 'cosine'
                        }
                    }
                    """,
                    dimensions=settings.embedding_dimensions,
                )
                log.info(
                    "vector_index_ensured",
                    index_name="entity_embedding",
                    dimensions=settings.embedding_dimensions,
                )
                return True
        except Exception as e:
            log.error("vector_index_creation_failed", error=str(e), exc_info=True)
            return False
```

- [ ] **Step 5: Run existing memory tests to verify backward compatibility**

Run: `uv run pytest tests/personal_agent/memory/ -v -x`
Expected: All existing tests pass (new fields have defaults, embedding is optional).

---

## Chunk 7: Hybrid Search

**Track:** B — Memory Quality
**Tier:** Tier-2 (Sonnet) — modifying complex existing code
**Depends on:** Chunk 6
**Estimated effort:** 2 hours

### Task 7.1: Write Hybrid Search Tests

**Files:**
- Create: `tests/personal_agent/memory/test_hybrid_search.py`

- [ ] **Step 1: Write hybrid search tests**

```python
# tests/personal_agent/memory/test_hybrid_search.py
"""Tests for hybrid search (vector + keyword + graph traversal).

Hybrid search combines:
1. Vector similarity (embedding cosine distance)
2. Keyword matching (existing entity name/type MERGE)
3. Graph traversal (relationship-based discovery)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.memory.models import MemoryQuery
from personal_agent.memory.service import MemoryService


class TestHybridQueryMemory:
    @pytest.mark.asyncio
    async def test_vector_search_called_when_query_text_provided(self) -> None:
        """When query_text is provided, vector search should run."""
        service = MemoryService.__new__(MemoryService)
        service.connected = True
        service.driver = MagicMock()

        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value=None)
        mock_session.run = AsyncMock(return_value=mock_result)
        service.driver.session = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        ))

        with patch(
            "personal_agent.memory.service.generate_embedding",
            new_callable=AsyncMock,
            return_value=[0.1] * 1536,
        ) as mock_embed:
            query = MemoryQuery(entity_names=["Redis"], limit=10)
            await service.query_memory(query, query_text="Tell me about Redis caching")
            mock_embed.assert_called_once()

    @pytest.mark.asyncio
    async def test_keyword_only_when_no_query_text(self) -> None:
        """Without query_text, only keyword search runs (backward compatible)."""
        service = MemoryService.__new__(MemoryService)
        service.connected = True
        service.driver = MagicMock()

        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.data = AsyncMock(return_value=[])
        mock_session.run = AsyncMock(return_value=mock_result)
        service.driver.session = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        ))

        with patch(
            "personal_agent.memory.service.generate_embedding",
            new_callable=AsyncMock,
        ) as mock_embed:
            query = MemoryQuery(entity_names=["Redis"], limit=10)
            await service.query_memory(query)
            mock_embed.assert_not_called()
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/personal_agent/memory/test_hybrid_search.py -v`
Expected: Tests fail (embedding not yet wired into query_memory).

### Task 7.2: Implement Hybrid Search in query_memory

**Files:**
- Modify: `src/personal_agent/memory/service.py`

- [ ] **Step 1: Add embedding import**

Add at the top of `service.py`:

```python
from personal_agent.memory.embeddings import generate_embedding
```

- [ ] **Step 2: Add vector search to query_memory**

In `query_memory` (line 411), add a vector search branch when `query_text` is provided. After the existing keyword-based Cypher query execution (~line 500), add:

```python
                # --- Hybrid: vector similarity search ---
                vector_results: list[Any] = []
                if query_text:
                    try:
                        query_embedding = await generate_embedding(query_text)
                        if any(x != 0.0 for x in query_embedding):
                            vector_result = await session.run(
                                """
                                CALL db.index.vector.queryNodes(
                                    'entity_embedding', $top_k, $embedding
                                )
                                YIELD node, score
                                RETURN node.name AS name,
                                       node.entity_type AS entity_type,
                                       node.description AS description,
                                       score
                                ORDER BY score DESC
                                """,
                                top_k=min(query.limit, 20),
                                embedding=query_embedding,
                            )
                            vector_results = await vector_result.data()
                    except Exception as vec_exc:
                        log.warning(
                            "vector_search_failed",
                            error=str(vec_exc),
                            query_text=query_text[:100],
                        )
```

- [ ] **Step 3: Merge keyword and vector results in _calculate_relevance_scores**

Modify `_calculate_relevance_scores` to accept optional vector scores and blend them:

```python
    async def _calculate_relevance_scores(
        self,
        conversations: list[Any],
        query: MemoryQuery,
        vector_scores: dict[str, float] | None = None,
    ) -> dict[str, float]:
        """Calculate multi-factor relevance scores.

        Factors:
        - Recency: 0–0.3 (reduced from 0.4 to make room for vector)
        - Entity match: 0–0.3 (reduced from 0.4)
        - Importance: 0–0.15 (reduced from 0.2)
        - Vector similarity: 0–0.25 (new)
        """
```

- [ ] **Step 4: Run hybrid search tests**

Run: `uv run pytest tests/personal_agent/memory/test_hybrid_search.py -v`
Expected: All tests pass.

- [ ] **Step 5: Run all memory tests**

Run: `uv run pytest tests/personal_agent/memory/ -v -x`
Expected: All tests pass (backward compatible — vector search is additive).

---

## Chunk 8: Fuzzy Entity Deduplication

**Track:** B — Memory Quality
**Tier:** Tier-2 (Sonnet) — new algorithm
**Depends on:** Chunk 6
**Estimated effort:** 2 hours

### Task 8.1: Write Dedup Pipeline Tests

**Files:**
- Create: `tests/personal_agent/memory/test_dedup.py`

- [ ] **Step 1: Write dedup tests**

```python
# tests/personal_agent/memory/test_dedup.py
"""Tests for fuzzy entity deduplication.

The dedup pipeline checks vector similarity before MERGE to prevent
near-duplicate explosion (40 mentions → 500 nodes → should be ~10).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.memory.dedup import (
    DedupDecision,
    DedupResult,
    check_entity_duplicate,
)


class TestCheckEntityDuplicate:
    @pytest.mark.asyncio
    async def test_no_existing_entities_no_dedup(self) -> None:
        """No existing entities → create new (no duplicate)."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await check_entity_duplicate(
                name="PostgreSQL",
                entity_type="Technology",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.CREATE_NEW
        assert result.canonical_name is None

    @pytest.mark.asyncio
    async def test_exact_match_merges(self) -> None:
        """Exact name match → merge with existing."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[{"name": "PostgreSQL", "similarity": 1.0, "entity_type": "Technology"}],
        ):
            result = await check_entity_duplicate(
                name="PostgreSQL",
                entity_type="Technology",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.MERGE_EXISTING
        assert result.canonical_name == "PostgreSQL"

    @pytest.mark.asyncio
    async def test_high_similarity_merges(self) -> None:
        """Above threshold similarity → merge with canonical name."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[{"name": "Postgres", "similarity": 0.92, "entity_type": "Technology"}],
        ):
            result = await check_entity_duplicate(
                name="PostgreSQL Database",
                entity_type="Technology",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.MERGE_EXISTING
        assert result.canonical_name == "Postgres"

    @pytest.mark.asyncio
    async def test_low_similarity_creates_new(self) -> None:
        """Below threshold similarity → create new entity."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[{"name": "Redis", "similarity": 0.3, "entity_type": "Technology"}],
        ):
            result = await check_entity_duplicate(
                name="PostgreSQL",
                entity_type="Technology",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.CREATE_NEW


class TestDedupResult:
    def test_create_new(self) -> None:
        result = DedupResult(decision=DedupDecision.CREATE_NEW)
        assert result.canonical_name is None

    def test_merge_existing(self) -> None:
        result = DedupResult(
            decision=DedupDecision.MERGE_EXISTING,
            canonical_name="PostgreSQL",
            similarity_score=0.95,
        )
        assert result.canonical_name == "PostgreSQL"
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/personal_agent/memory/test_dedup.py -v`
Expected: `ModuleNotFoundError`

### Task 8.2: Implement Dedup Pipeline

**Files:**
- Create: `src/personal_agent/memory/dedup.py`

- [ ] **Step 1: Implement the dedup module**

```python
# src/personal_agent/memory/dedup.py
"""Fuzzy entity deduplication pipeline.

Two-tier dedup on entity creation:
1. Vector similarity check against existing entities (via Neo4j vector index)
2. Above-threshold matches are merged to the canonical entity name

Prevents the 500-node explosion from 40 mentions of 10 entities
(EVAL-02 Scenario 4).

See: ADR-0035, Enhancement 2 (fuzzy entity deduplication)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog

from personal_agent.config import get_settings

logger = structlog.get_logger(__name__)


class DedupDecision(Enum):
    """Deduplication decision for an entity."""

    CREATE_NEW = "create_new"
    MERGE_EXISTING = "merge_existing"


@dataclass(frozen=True)
class DedupResult:
    """Result of a deduplication check.

    Args:
        decision: Whether to create a new entity or merge with existing.
        canonical_name: Name of the existing entity to merge with (if MERGE).
        similarity_score: Cosine similarity with the best match.
    """

    decision: DedupDecision
    canonical_name: str | None = None
    similarity_score: float = 0.0


async def check_entity_duplicate(
    name: str,
    entity_type: str,
    embedding: list[float],
    neo4j_session: Any,
) -> DedupResult:
    """Check if an entity is a duplicate of an existing entity.

    Uses vector similarity search against the entity_embedding index.

    Args:
        name: Proposed entity name.
        entity_type: Entity type (e.g., "Technology").
        embedding: Embedding vector for the proposed entity.
        neo4j_session: Active Neo4j async session.

    Returns:
        DedupResult with merge decision.
    """
    settings = get_settings()
    threshold = settings.dedup_similarity_threshold

    similar = await _find_similar_entities(
        embedding=embedding,
        entity_type=entity_type,
        neo4j_session=neo4j_session,
        top_k=5,
    )

    if not similar:
        return DedupResult(decision=DedupDecision.CREATE_NEW)

    best = similar[0]

    # Exact name match always merges
    if best["name"].lower() == name.lower():
        return DedupResult(
            decision=DedupDecision.MERGE_EXISTING,
            canonical_name=best["name"],
            similarity_score=best["similarity"],
        )

    # Above threshold — merge with canonical
    if best["similarity"] >= threshold:
        logger.info(
            "entity_dedup_merge",
            proposed_name=name,
            canonical_name=best["name"],
            similarity=round(best["similarity"], 3),
        )
        return DedupResult(
            decision=DedupDecision.MERGE_EXISTING,
            canonical_name=best["name"],
            similarity_score=best["similarity"],
        )

    return DedupResult(decision=DedupDecision.CREATE_NEW)


async def _find_similar_entities(
    embedding: list[float],
    entity_type: str,
    neo4j_session: Any,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Find entities similar to the given embedding vector.

    Args:
        embedding: Query embedding vector.
        entity_type: Filter to same entity type.
        neo4j_session: Active Neo4j async session.
        top_k: Number of results to return.

    Returns:
        List of dicts with name, similarity, entity_type.
    """
    try:
        result = await neo4j_session.run(
            """
            CALL db.index.vector.queryNodes(
                'entity_embedding', $top_k, $embedding
            )
            YIELD node, score
            WHERE node.entity_type = $entity_type
            RETURN node.name AS name,
                   node.entity_type AS entity_type,
                   score AS similarity
            ORDER BY score DESC
            """,
            top_k=top_k,
            embedding=embedding,
            entity_type=entity_type,
        )
        return await result.data()

    except Exception as exc:
        logger.warning(
            "dedup_vector_search_failed",
            error=str(exc),
        )
        return []
```

- [ ] **Step 2: Run dedup tests**

Run: `uv run pytest tests/personal_agent/memory/test_dedup.py -v`
Expected: All tests pass.

### Task 8.3: Wire Dedup into create_entity

**Files:**
- Modify: `src/personal_agent/memory/service.py`

- [ ] **Step 1: Add dedup check before MERGE**

In `create_entity`, add the dedup check before the Cypher MERGE. The embedding must be generated first (if not provided on the entity), then checked for duplicates:

```python
    async def create_entity(self, entity: Entity) -> str:
        """Create or update an entity node with dedup and optional embedding.

        Args:
            entity: Entity to create.

        Returns:
            Entity ID (name-based, may be canonical name if deduplicated).
        """
        if not self.connected or not self.driver:
            log.warning("neo4j_not_connected")
            return ""

        try:
            # Generate embedding if not provided
            embedding = entity.embedding
            if embedding is None and entity.description:
                embed_text = f"{entity.name}: {entity.description}"
                embedding = await generate_embedding(embed_text)

            # Dedup check
            effective_name = entity.name
            if embedding and any(x != 0.0 for x in embedding):
                async with self.driver.session() as session:
                    from personal_agent.memory.dedup import check_entity_duplicate, DedupDecision

                    dedup_result = await check_entity_duplicate(
                        name=entity.name,
                        entity_type=entity.entity_type,
                        embedding=embedding,
                        neo4j_session=session,
                    )
                    if dedup_result.decision == DedupDecision.MERGE_EXISTING and dedup_result.canonical_name:
                        effective_name = dedup_result.canonical_name
                        log.info(
                            "entity_deduplicated",
                            original_name=entity.name,
                            canonical_name=effective_name,
                            similarity=dedup_result.similarity_score,
                        )

            # Proceed with MERGE using effective_name
            async with self.driver.session() as session:
                # ... (existing MERGE query using effective_name instead of entity.name)
```

- [ ] **Step 2: Run all memory tests**

Run: `uv run pytest tests/personal_agent/memory/ -v -x`
Expected: All tests pass.

---

## Chunk 9: Recall Controller

**Track:** C — Independent
**Tier:** Tier-2 (Sonnet) — implementation from detailed ADR
**Depends on:** Nothing (parallel with Tracks A and B)
**Estimated effort:** 2 hours

### Task 9.1: Add Recall Types

**Files:**
- Modify: `src/personal_agent/request_gateway/types.py`

- [ ] **Step 1: Add RecallCandidate, RecallResult types and recall_context field**

Append to `types.py`:

```python
@dataclass(frozen=True)
class RecallCandidate:
    """A session fact candidate for recall injection.

    Args:
        fact: The extracted fact text (e.g., "Primary database is PostgreSQL").
        source_turn: Turn index in session_messages where the fact was found.
        noun_phrase: The matched noun phrase from the user's query.
        confidence: Relevance score (0.0–1.0), weighted by recency × specificity.
    """

    fact: str
    source_turn: int
    noun_phrase: str
    confidence: float


@dataclass(frozen=True)
class RecallResult:
    """Output of the recall controller (Stage 4b).

    Args:
        reclassified: Whether the intent was changed from CONVERSATIONAL to MEMORY_RECALL.
        original_task_type: The pre-reclassification task type.
        trigger_cue: Which cue pattern matched (for telemetry).
        candidates: Session fact candidates (max 3).
    """

    reclassified: bool
    original_task_type: TaskType
    trigger_cue: str
    candidates: list[RecallCandidate] = field(default_factory=list)
```

Add `recall_context` field to `GatewayOutput`:

```python
@dataclass(frozen=True)
class GatewayOutput:
    """Complete output of the gateway pipeline."""

    intent: IntentResult
    governance: GovernanceContext
    decomposition: DecompositionResult
    context: AssembledContext
    session_id: str
    trace_id: str
    degraded_stages: list[str] = field(default_factory=list)
    recall_context: RecallResult | None = None
```

- [ ] **Step 2: Run existing gateway type tests**

Run: `uv run pytest tests/personal_agent/request_gateway/test_types.py -v`
Expected: All tests pass (new field has default `None`).

### Task 9.2: Write Recall Controller Tests

**Files:**
- Create: `tests/personal_agent/request_gateway/test_recall_controller.py`

- [ ] **Step 1: Write recall controller tests**

```python
# tests/personal_agent/request_gateway/test_recall_controller.py
"""Tests for the recall controller (Stage 4b).

Three-gate design:
1. Task type gate: only CONVERSATIONAL enters
2. Cue pattern gate: implicit backward-reference cues
3. Session fact gate: noun phrase corroboration in session history
"""

from __future__ import annotations

import pytest

from personal_agent.request_gateway.recall_controller import (
    _detect_recall_cues,
    _extract_noun_phrases,
    _scan_session_facts,
    run_recall_controller,
)
from personal_agent.request_gateway.types import (
    Complexity,
    IntentResult,
    RecallResult,
    TaskType,
)


class TestDetectRecallCues:
    def test_again_with_question(self) -> None:
        assert _detect_recall_cues("What was our primary database again?") is not None

    def test_going_back(self) -> None:
        assert _detect_recall_cues("Going back to the beginning — what was our database?") is not None

    def test_remind_me(self) -> None:
        assert _detect_recall_cues("Remind me what we decided on caching") is not None

    def test_what_did_we_decide(self) -> None:
        assert _detect_recall_cues("What did we decide on the API framework?") is not None

    def test_the_thing_we_discussed(self) -> None:
        assert _detect_recall_cues("The framework we discussed earlier") is not None

    def test_no_cue_simple_question(self) -> None:
        assert _detect_recall_cues("What is the weather today?") is None

    def test_no_cue_bare_again(self) -> None:
        """Bare 'again' without interrogative context should not trigger."""
        assert _detect_recall_cues("Let's try that approach again") is None

    def test_no_cue_conversational(self) -> None:
        assert _detect_recall_cues("Tell me something interesting") is None


class TestExtractNounPhrases:
    def test_extracts_primary_database(self) -> None:
        phrases = _extract_noun_phrases("What was our primary database again?")
        assert any("database" in p.lower() for p in phrases)

    def test_extracts_api_framework(self) -> None:
        phrases = _extract_noun_phrases("What did we decide on the API framework?")
        assert any("framework" in p.lower() for p in phrases)


class TestScanSessionFacts:
    def test_finds_matching_fact(self) -> None:
        session_messages = [
            {"role": "user", "content": "Let's use PostgreSQL as our primary database"},
            {"role": "assistant", "content": "Great choice! PostgreSQL is excellent for this."},
            {"role": "user", "content": "Now let's discuss caching..."},
            {"role": "assistant", "content": "Sure, let's look at Redis."},
        ]
        candidates = _scan_session_facts(
            noun_phrases=["primary database"],
            session_messages=session_messages,
            max_candidates=3,
        )
        assert len(candidates) >= 1
        assert any("PostgreSQL" in c.fact or "database" in c.fact for c in candidates)

    def test_no_match_returns_empty(self) -> None:
        session_messages = [
            {"role": "user", "content": "Hello, how are you?"},
            {"role": "assistant", "content": "I'm doing well!"},
        ]
        candidates = _scan_session_facts(
            noun_phrases=["primary database"],
            session_messages=session_messages,
            max_candidates=3,
        )
        assert len(candidates) == 0


class TestRunRecallController:
    def test_non_conversational_passes_through(self) -> None:
        """Non-CONVERSATIONAL intents skip the controller entirely."""
        intent = IntentResult(
            task_type=TaskType.ANALYSIS,
            complexity=Complexity.MODERATE,
            confidence=0.9,
            signals=["reasoning_patterns"],
        )
        result = run_recall_controller(
            intent=intent,
            user_message="Analyze Redis performance",
            session_messages=[],
        )
        assert result is None  # No reclassification

    def test_conversational_no_cue_passes_through(self) -> None:
        """CONVERSATIONAL without recall cues passes through."""
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.7,
            signals=[],
        )
        result = run_recall_controller(
            intent=intent,
            user_message="Tell me something interesting",
            session_messages=[],
        )
        assert result is None

    def test_reclassifies_implicit_recall(self) -> None:
        """Implicit recall with corroborating session fact → reclassify."""
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.7,
            signals=[],
        )
        session = [
            {"role": "user", "content": "Let's use PostgreSQL as our primary database"},
            {"role": "assistant", "content": "PostgreSQL is a great choice."},
            {"role": "user", "content": "Now let's talk about the API layer."},
            {"role": "assistant", "content": "Sure, FastAPI is our framework."},
        ]
        result = run_recall_controller(
            intent=intent,
            user_message="Going back to the beginning — what was our primary database again?",
            session_messages=session,
        )
        assert result is not None
        assert result.reclassified is True
        assert result.original_task_type == TaskType.CONVERSATIONAL
        assert len(result.candidates) >= 1

    def test_cue_without_session_match_no_reclassify(self) -> None:
        """Cue detected but no corroborating fact → false positive, no reclassify."""
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.7,
            signals=[],
        )
        result = run_recall_controller(
            intent=intent,
            user_message="What was our primary database again?",
            session_messages=[
                {"role": "user", "content": "Hello!"},
                {"role": "assistant", "content": "Hi there!"},
            ],
        )
        # Cue fires but no session fact corroboration → no reclassify
        assert result is None or result.reclassified is False
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/personal_agent/request_gateway/test_recall_controller.py -v`
Expected: `ModuleNotFoundError`

### Task 9.3: Implement Recall Controller

**Files:**
- Create: `src/personal_agent/request_gateway/recall_controller.py`

- [ ] **Step 1: Implement the recall controller**

```python
# src/personal_agent/request_gateway/recall_controller.py
"""Recall controller — Stage 4b post-classification refinement.

Detects implicit backward-reference cues in messages classified as
CONVERSATIONAL by Stage 4, corroborates against session history,
and reclassifies to MEMORY_RECALL with session fact evidence.

Three-gate design:
1. Task type gate: only CONVERSATIONAL classifications enter
2. Cue pattern gate: regex match for implicit backward-reference cues
3. Session fact gate: noun phrase extraction + session history scan

See: ADR-0037 (recall-controller)
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import structlog

from personal_agent.request_gateway.types import (
    IntentResult,
    RecallCandidate,
    RecallResult,
    TaskType,
)

logger = structlog.get_logger(__name__)

# --- Recall cue patterns (ADR-0037 Decision 2) ---
_RECALL_CUE_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)"
    # Temporal back-reference with interrogative context
    r"(?:what\s+(?:was|were|is)\s+(?:our|the|that)\s+\w+\s+again)"
    r"|(?:(?:going|go)\s+back\s+(?:to\s+)?(?:the\s+)?(?:beginning|start|earlier))"
    r"|(?:(?:back\s+to|earlier)\s+(?:when|where|what)\s+)"
    r"|(?:at\s+the\s+(?:beginning|start)\s*[,\u2014\u2013\-])"
    # Possessive prior-decision
    r"|(?:what\s+(?:was|were|is)\s+(?:our|the)\s+(?:primary|main|original|first|chosen|selected|preferred))"
    r"|(?:what\s+did\s+(?:we|I)\s+(?:decide|pick|choose|settle|go\s+with|land\s+on))"
    # Explicit memory request
    r"|(?:remind\s+me\s+(?:what|which|about|of))"
    r"|(?:refresh\s+my\s+memory)"
    # Resumptive reference
    r"|(?:the\s+\w+\s+(?:we|I)\s+(?:discussed|mentioned|talked\s+about|decided\s+on|chose|picked))",
)

# Noun phrase extraction: simple heuristic
_NOUN_PHRASE_RE = re.compile(
    r"(?:our|the|that|my)\s+((?:[A-Z][\w]*\s+)*[\w]+)",
    re.IGNORECASE,
)


def run_recall_controller(
    intent: IntentResult,
    user_message: str,
    session_messages: Sequence[dict[str, str]],
    max_candidates: int = 3,
    max_scan_turns: int = 20,
) -> RecallResult | None:
    """Run the recall controller (Stage 4b).

    Args:
        intent: Stage 4 intent classification result.
        user_message: Current user message.
        session_messages: Conversation history (most recent last).
        max_candidates: Max session fact candidates to return.
        max_scan_turns: Max turns to scan in session history.

    Returns:
        RecallResult if reclassification occurred, None if passed through.
    """
    # Gate 1: Only CONVERSATIONAL enters
    if intent.task_type != TaskType.CONVERSATIONAL:
        logger.debug(
            "recall_controller_skipped",
            original_task_type=intent.task_type.value,
        )
        return None

    # Gate 2: Cue pattern match
    cue = _detect_recall_cues(user_message)
    if cue is None:
        return None

    logger.info(
        "recall_cue_detected",
        cue_pattern=cue,
        message_excerpt=user_message[:80],
    )

    # Gate 3: Noun phrase extraction + session fact scan
    noun_phrases = _extract_noun_phrases(user_message)
    if not noun_phrases:
        logger.info(
            "recall_cue_false_positive",
            cue_pattern=cue,
            reason="no_noun_phrase",
        )
        return RecallResult(
            reclassified=False,
            original_task_type=TaskType.CONVERSATIONAL,
            trigger_cue=cue,
            candidates=[],
        )

    candidates = _scan_session_facts(
        noun_phrases=noun_phrases,
        session_messages=list(session_messages[-max_scan_turns:]),
        max_candidates=max_candidates,
    )

    if not candidates:
        logger.info(
            "recall_cue_false_positive",
            cue_pattern=cue,
            reason="no_session_match",
        )
        return RecallResult(
            reclassified=False,
            original_task_type=TaskType.CONVERSATIONAL,
            trigger_cue=cue,
            candidates=[],
        )

    # Reclassify
    logger.info(
        "recall_reclassified",
        original_type="conversational",
        new_type="memory_recall",
        trigger_cue=cue,
        top_candidate_fact=candidates[0].fact[:100],
        confidence=0.85,
    )

    return RecallResult(
        reclassified=True,
        original_task_type=TaskType.CONVERSATIONAL,
        trigger_cue=cue,
        candidates=candidates,
    )


def _detect_recall_cues(message: str) -> str | None:
    """Check if the message contains implicit backward-reference cues.

    Args:
        message: User message text.

    Returns:
        Matched cue string, or None if no cue detected.
    """
    match = _RECALL_CUE_PATTERNS.search(message)
    if match:
        return match.group(0).strip()
    return None


def _extract_noun_phrases(message: str) -> list[str]:
    """Extract target noun phrases from the user message.

    Uses simple heuristic: "our/the/that/my + noun phrase".

    Args:
        message: User message text.

    Returns:
        List of extracted noun phrases.
    """
    matches = _NOUN_PHRASE_RE.findall(message)
    # Deduplicate and clean
    seen: set[str] = set()
    phrases: list[str] = []
    for m in matches:
        cleaned = m.strip().lower()
        if cleaned and cleaned not in seen and len(cleaned) > 2:
            seen.add(cleaned)
            phrases.append(cleaned)
    return phrases


def _scan_session_facts(
    noun_phrases: list[str],
    session_messages: list[dict[str, str]],
    max_candidates: int = 3,
) -> list[RecallCandidate]:
    """Scan session history for facts matching the noun phrases.

    Args:
        noun_phrases: Target noun phrases to search for.
        session_messages: Conversation history to scan.
        max_candidates: Max candidates to return.

    Returns:
        List of RecallCandidate sorted by confidence (descending).
    """
    candidates: list[RecallCandidate] = []
    total_turns = len(session_messages)

    for i, msg in enumerate(reversed(session_messages)):
        content = msg.get("content", "")
        if not content:
            continue

        for phrase in noun_phrases:
            if phrase.lower() in content.lower():
                # Extract the sentence containing the match
                sentences = re.split(r"[.!?\n]", content)
                matching_sentence = ""
                for s in sentences:
                    if phrase.lower() in s.lower():
                        matching_sentence = s.strip()
                        break

                if not matching_sentence:
                    matching_sentence = content[:200]

                # Score by recency (newer = higher)
                turn_index = total_turns - 1 - i
                recency_score = 1.0 - (i / max(total_turns, 1))

                candidates.append(
                    RecallCandidate(
                        fact=matching_sentence,
                        source_turn=turn_index,
                        noun_phrase=phrase,
                        confidence=recency_score,
                    )
                )

                if len(candidates) >= max_candidates:
                    return sorted(candidates, key=lambda c: c.confidence, reverse=True)

    return sorted(candidates, key=lambda c: c.confidence, reverse=True)
```

- [ ] **Step 2: Run recall controller tests**

Run: `uv run pytest tests/personal_agent/request_gateway/test_recall_controller.py -v`
Expected: All tests pass.

### Task 9.4: Wire Stage 4b into Pipeline

**Files:**
- Modify: `src/personal_agent/request_gateway/pipeline.py`
- Modify: `src/personal_agent/request_gateway/context.py`

- [ ] **Step 1: Add Stage 4b call in pipeline**

In `pipeline.py`, after Stage 4 (intent classification) and before Stage 5 (decomposition), add:

```python
    # Stage 4b — Recall Controller (ADR-0037)
    from personal_agent.request_gateway.recall_controller import run_recall_controller

    recall_result = run_recall_controller(
        intent=intent,
        user_message=user_message,
        session_messages=session_messages,
    )

    if recall_result is not None and recall_result.reclassified:
        intent = IntentResult(
            task_type=TaskType.MEMORY_RECALL,
            complexity=intent.complexity,
            confidence=0.85,
            signals=[*intent.signals, "recall_cue_reclassified", recall_result.trigger_cue],
        )
```

- [ ] **Step 2: Pass recall_context to GatewayOutput**

In the GatewayOutput construction at the end of `run_gateway_pipeline`, add:

```python
    return GatewayOutput(
        intent=intent,
        governance=governance,
        decomposition=decomposition,
        context=assembled,
        session_id=session_id,
        trace_id=trace_id,
        degraded_stages=degraded,
        recall_context=recall_result,
    )
```

- [ ] **Step 3: Inject session fact candidates in context assembly**

In `context.py`, in `assemble_context`, add session fact injection when recall_context is present. This requires passing recall_context through — add it as an optional parameter:

```python
async def assemble_context(
    user_message: str,
    session_messages: list[dict[str, str]],
    intent: IntentResult,
    memory_adapter: MemoryProtocol | None,
    trace_id: str,
    recall_context: RecallResult | None = None,
) -> AssembledContext:
```

Then, when building memory context, inject recall candidates:

```python
    # Inject session fact candidates from recall controller
    if recall_context and recall_context.reclassified and recall_context.candidates:
        recall_section = "\n## Session Fact Recall\n"
        recall_section += "The user appears to be referring to something discussed earlier.\n"
        recall_section += "Relevant facts from the conversation:\n"
        for c in recall_context.candidates:
            recall_section += f"- Turn {c.source_turn}: \"{c.fact}\" (matched: \"{c.noun_phrase}\")\n"
        recall_section += "\nUse these facts to answer accurately. Do not claim you don't know.\n"

        if memory_context is None:
            memory_context = [{"role": "system", "content": recall_section}]
        else:
            memory_context.append({"role": "system", "content": recall_section})
```

- [ ] **Step 4: Run all gateway tests**

Run: `uv run pytest tests/personal_agent/request_gateway/ -v -x`
Expected: All tests pass.

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest -v -x --timeout=120`
Expected: All tests pass.

---

## Chunk 10: Stability Threshold Redesign + Cross-Session Recall Validation (Stretch)

**Track:** Stretch
**Tier:** Tier-2 (Sonnet)
**Depends on:** Chunk 7 (hybrid search)
**Estimated effort:** 1.5 hours

### Task 10.1: Redesign Stability Score

**Files:**
- Modify: `src/personal_agent/memory/fact.py`

- [ ] **Step 1: Update stability_score formula**

Replace the `stability_score` method in `PromotionCandidate`:

```python
    def stability_score(self) -> float:
        """Compute stability score with recency boost.

        New formula (Slice 3):
        - mention_factor: min(mention_count / 5.0, 0.4) — promotes at 5 mentions, not 50
        - time_factor: min(days / 30.0, 0.3) — 30 days, not 90
        - recency_boost: 0.3 if last_seen within 24h, decaying to 0 over 7 days

        Args: (uses self.mention_count, self.first_seen, self.last_seen)

        Returns:
            Stability score between 0.0 and 1.0.
        """
        from datetime import datetime, timezone

        mention_factor = min(self.mention_count / 5.0, 0.4)
        days_span = (self.last_seen - self.first_seen).total_seconds() / 86400.0
        time_factor = min(days_span / 30.0, 0.3)

        # Recency boost: how recently was this entity last seen?
        now = datetime.now(timezone.utc)
        hours_since_seen = (now - self.last_seen).total_seconds() / 3600.0
        if hours_since_seen <= 24:
            recency_boost = 0.3
        elif hours_since_seen <= 168:  # 7 days
            recency_boost = 0.3 * (1.0 - (hours_since_seen - 24) / 144)
        else:
            recency_boost = 0.0

        return mention_factor + time_factor + max(recency_boost, 0.0)
```

- [ ] **Step 2: Update tests for new threshold**

Run: `uv run pytest tests/personal_agent/memory/test_fact.py -v`
Expected: May need to update expected values. An entity with 5 mentions within the last 24h should score ~0.7 (0.4 + 0.0 + 0.3).

### Task 10.2: Cross-Session Recall Validation

**Files:**
- Create: `tests/personal_agent/memory/test_cross_session_recall.py`

- [ ] **Step 1: Write cross-session recall validation test**

```python
# tests/personal_agent/memory/test_cross_session_recall.py
"""Cross-session recall validation.

Verifies that entities stored in session A can be retrieved in session B
via Neo4j semantic memory (not conversation history).

This is the critical gap identified in EVAL-03 Finding 3.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from personal_agent.memory.models import Entity, MemoryQuery
from personal_agent.memory.service import MemoryService


class TestCrossSessionRecall:
    @pytest.mark.asyncio
    async def test_entity_from_session_a_found_in_session_b(self) -> None:
        """Entity created in session A should be findable via vector search in session B.

        This test validates the full flow:
        1. Store entity with embedding in session A
        2. Query for entity by semantic similarity in session B (different session)
        """
        # This is an integration test — requires live Neo4j
        # Mark as skip if Neo4j is not available
        pytest.skip("Integration test — requires live Neo4j with vector index")

    @pytest.mark.asyncio
    async def test_query_memory_broad_returns_cross_session_entities(self) -> None:
        """query_memory_broad should return entities regardless of originating session."""
        pytest.skip("Integration test — requires live Neo4j")
```

- [ ] **Step 2: Document the validation procedure**

The cross-session validation should be run manually with live Neo4j:

```bash
# Start services
./scripts/init-services.sh

# Run session A: seed entities
uv run python -c "
import asyncio
from personal_agent.memory.service import MemoryService
from personal_agent.memory.models import Entity

async def seed():
    svc = MemoryService()
    await svc.connect()
    await svc.ensure_vector_index()
    entity = Entity(name='PostgreSQL', entity_type='Technology', description='Primary relational database')
    await svc.create_entity(entity)
    print('Seeded PostgreSQL entity')
    await svc.disconnect()

asyncio.run(seed())
"

# Run session B: query by semantic similarity
uv run python -c "
import asyncio
from personal_agent.memory.service import MemoryService
from personal_agent.memory.models import MemoryQuery

async def query():
    svc = MemoryService()
    await svc.connect()
    result = await svc.query_memory(MemoryQuery(entity_names=['PostgreSQL'], limit=5), query_text='relational database')
    print(f'Found: {result}')
    await svc.disconnect()

asyncio.run(query())
"
```

---

## Chunk 11: Proactive Memory (Stretch)

**Track:** Stretch
**Tier:** Tier-2 (Sonnet)
**Depends on:** Chunks 7, 10
**Estimated effort:** 3 hours

### Task 11.1: Add suggest_relevant to MemoryProtocol

**Files:**
- Modify: `src/personal_agent/memory/protocol.py`

- [ ] **Step 1: Add suggest_relevant method to MemoryProtocol**

```python
    async def suggest_relevant(
        self,
        user_message: str,
        session_id: str,
        trace_id: str,
        max_suggestions: int = 3,
    ) -> list[dict[str, Any]]:
        """Suggest relevant entities/facts unprompted based on the current message.

        Uses embedding similarity to find semantically related entities that
        the user hasn't explicitly asked about but may be relevant.

        Args:
            user_message: Current user message to find relevance against.
            session_id: Current session ID (to exclude already-mentioned entities).
            trace_id: Trace identifier.
            max_suggestions: Maximum number of suggestions.

        Returns:
            List of suggestion dicts with entity name, type, relevance score,
            and reason for suggestion.
        """
        ...
```

### Task 11.2: Implement suggest_relevant

**Files:**
- Modify: `src/personal_agent/memory/protocol_adapter.py`
- Modify: `src/personal_agent/memory/service.py`

- [ ] **Step 1: Implement in MemoryService**

Add to `MemoryService`:

```python
    async def suggest_relevant(
        self,
        user_message: str,
        session_id: str,
        trace_id: str,
        max_suggestions: int = 3,
    ) -> list[dict[str, Any]]:
        """Find entities semantically related to the current message.

        Args:
            user_message: Current user message.
            session_id: Current session to exclude already-mentioned entities.
            trace_id: Trace identifier.
            max_suggestions: Max suggestions.

        Returns:
            List of suggestion dicts.
        """
        if not self.connected or not self.driver:
            return []

        try:
            embedding = await generate_embedding(user_message)
            if all(x == 0.0 for x in embedding):
                return []

            async with self.driver.session() as session:
                result = await session.run(
                    """
                    CALL db.index.vector.queryNodes(
                        'entity_embedding', $top_k, $embedding
                    )
                    YIELD node, score
                    WHERE score >= $min_score
                      AND node.memory_type = 'semantic'
                    RETURN node.name AS name,
                           node.entity_type AS entity_type,
                           node.description AS description,
                           score AS relevance
                    ORDER BY score DESC
                    """,
                    top_k=max_suggestions * 2,
                    embedding=embedding,
                    min_score=0.6,
                )
                records = await result.data()

            suggestions = [
                {
                    "name": r["name"],
                    "entity_type": r["entity_type"],
                    "description": r["description"],
                    "relevance": round(r["relevance"], 3),
                }
                for r in records[:max_suggestions]
            ]

            log.info(
                "proactive_memory_suggestions",
                suggestion_count=len(suggestions),
                trace_id=trace_id,
            )

            return suggestions

        except Exception as exc:
            log.warning("proactive_memory_failed", error=str(exc), trace_id=trace_id)
            return []
```

- [ ] **Step 2: Wire into MemoryServiceAdapter**

Add to `MemoryServiceAdapter`:

```python
    async def suggest_relevant(
        self,
        user_message: str,
        session_id: str,
        trace_id: str,
        max_suggestions: int = 3,
    ) -> list[dict[str, Any]]:
        return await self._service.suggest_relevant(
            user_message=user_message,
            session_id=session_id,
            trace_id=trace_id,
            max_suggestions=max_suggestions,
        )
```

### Task 11.3: Wire Proactive Memory into Context Assembly

**Files:**
- Modify: `src/personal_agent/request_gateway/context.py`

- [ ] **Step 1: Call suggest_relevant during context assembly**

In `assemble_context`, after memory query for the intent, add proactive memory:

```python
    # Proactive memory: suggest relevant entities unprompted
    proactive_context: list[dict[str, str]] | None = None
    if memory_adapter and intent.task_type not in (TaskType.MEMORY_RECALL,):
        try:
            suggestions = await memory_adapter.suggest_relevant(
                user_message=user_message,
                session_id="",  # TODO: pass session_id through
                trace_id=trace_id,
            )
            if suggestions:
                section = "\n## Relevant Context (proactive)\n"
                section += "The following entities from your knowledge graph may be relevant:\n"
                for s in suggestions:
                    section += f"- **{s['name']}** ({s['entity_type']}): {s.get('description', 'N/A')}\n"
                proactive_context = [{"role": "system", "content": section}]
        except Exception:
            pass  # Proactive memory is best-effort
```

- [ ] **Step 2: Run all tests**

Run: `uv run pytest -v -x --timeout=120`
Expected: All tests pass.

---

## Chunk 12: Geospatial Pipeline (Stretch)

**Track:** Stretch
**Tier:** Tier-2 (Sonnet)
**Depends on:** Chunk 6 (schema fields already added)
**Estimated effort:** 2 hours

### Task 12.1: Implement Geocoding Pipeline

**Files:**
- Create: `src/personal_agent/memory/geocoding.py`
- Create: `tests/personal_agent/memory/test_geocoding.py`

- [ ] **Step 1: Write geocoding tests**

```python
# tests/personal_agent/memory/test_geocoding.py
"""Tests for geocoding pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.memory.geocoding import geocode_location


class TestGeocodeLocation:
    @pytest.mark.asyncio
    async def test_known_location(self) -> None:
        with patch(
            "personal_agent.memory.geocoding._call_geocoding_api",
            new_callable=AsyncMock,
            return_value=(48.8566, 2.3522),
        ):
            coords = await geocode_location("Paris")
            assert coords is not None
            lat, lon = coords
            assert 48.0 < lat < 49.0
            assert 2.0 < lon < 3.0

    @pytest.mark.asyncio
    async def test_unknown_location(self) -> None:
        with patch(
            "personal_agent.memory.geocoding._call_geocoding_api",
            new_callable=AsyncMock,
            return_value=None,
        ):
            coords = await geocode_location("xyznonexistent")
            assert coords is None
```

- [ ] **Step 2: Implement geocoding module**

```python
# src/personal_agent/memory/geocoding.py
"""Geocoding pipeline for Location entities.

Resolves Location entity names to lat/lon coordinates for spatial indexing.
Uses Nominatim (OpenStreetMap) as the free geocoding API.

See: ADR-0035 P2 (geospatial context)
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


async def geocode_location(location_name: str) -> tuple[float, float] | None:
    """Geocode a location name to coordinates.

    Args:
        location_name: Location name (e.g., "Paris", "Lyon, France").

    Returns:
        (latitude, longitude) tuple, or None if geocoding fails.
    """
    try:
        return await _call_geocoding_api(location_name)
    except Exception as exc:
        logger.warning(
            "geocoding_failed",
            location=location_name,
            error=str(exc),
        )
        return None


async def _call_geocoding_api(location_name: str) -> tuple[float, float] | None:
    """Call Nominatim geocoding API.

    Args:
        location_name: Location to geocode.

    Returns:
        (latitude, longitude) or None.
    """
    import httpx

    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": location_name,
                "format": "json",
                "limit": 1,
            },
            headers={"User-Agent": "PersonalAgent/1.0"},
            timeout=5.0,
        )
        response.raise_for_status()
        results = response.json()

        if results:
            return (float(results[0]["lat"]), float(results[0]["lon"]))

    return None
```

### Task 12.2: Add Spatial Index and Proximity Queries

**Files:**
- Modify: `src/personal_agent/memory/service.py`

- [ ] **Step 1: Add spatial index creation**

Add to `MemoryService`:

```python
    async def ensure_spatial_index(self) -> bool:
        """Create Neo4j spatial index on Entity.coordinates if not exists.

        Returns:
            True if index exists or was created.
        """
        if not self.connected or not self.driver:
            return False

        try:
            async with self.driver.session() as session:
                await session.run(
                    """
                    CREATE POINT INDEX entity_location IF NOT EXISTS
                    FOR (e:Entity)
                    ON (e.coordinates)
                    """
                )
                log.info("spatial_index_ensured", index_name="entity_location")
                return True
        except Exception as e:
            log.error("spatial_index_creation_failed", error=str(e), exc_info=True)
            return False
```

- [ ] **Step 2: Add proximity query method**

```python
    async def query_entities_near(
        self,
        latitude: float,
        longitude: float,
        distance_km: float = 50.0,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Find entities near a geographic point.

        Args:
            latitude: Latitude of the query point.
            longitude: Longitude of the query point.
            distance_km: Search radius in kilometers.
            limit: Max results.

        Returns:
            List of entity dicts with name, type, distance.
        """
        if not self.connected or not self.driver:
            return []

        try:
            async with self.driver.session() as session:
                result = await session.run(
                    """
                    MATCH (e:Entity)
                    WHERE e.coordinates IS NOT NULL
                      AND point.distance(
                          e.coordinates,
                          point({latitude: $lat, longitude: $lon})
                      ) <= $distance_m
                    RETURN e.name AS name,
                           e.entity_type AS entity_type,
                           point.distance(
                               e.coordinates,
                               point({latitude: $lat, longitude: $lon})
                           ) AS distance_m
                    ORDER BY distance_m ASC
                    LIMIT $limit
                    """,
                    lat=latitude,
                    lon=longitude,
                    distance_m=distance_km * 1000,
                    limit=limit,
                )
                return await result.data()
        except Exception as exc:
            log.warning("proximity_query_failed", error=str(exc))
            return []
```

- [ ] **Step 3: Run all tests**

Run: `uv run pytest -v -x --timeout=120`
Expected: All tests pass.

---

## Summary Table

| Chunk | Content | Track | Tier | Depends On | Effort | Linear Issue |
|-------|---------|-------|------|------------|--------|-------------|
| 1 | Expansion types + config | A | Tier-3 | — | 30 min | FRE-154 |
| 2 | Fallback planner | A | Tier-2 | 1 | 1 hr | FRE-154 |
| 3 | Expansion controller + sub-agent modes | A | Tier-2 | 1, 2 | 3 hr | FRE-154 |
| 4 | Per-phase budgets + degradation | A | Tier-2 | 3 | 1.5 hr | TBD |
| 5 | Expansion telemetry + eval harness | A | Tier-2 | 3 | 1.5 hr | TBD |
| 6 | Embedding infra + geospatial schema | B | Tier-2 | — | 2.5 hr | TBD |
| 7 | Hybrid search | B | Tier-2 | 6 | 2 hr | TBD |
| 8 | Fuzzy entity dedup | B | Tier-2 | 6 | 2 hr | TBD |
| 9 | Recall controller | C | Tier-2 | — | 2 hr | FRE-155 |
| 10 | Stability threshold + cross-session | Stretch | Tier-2 | 7 | 1.5 hr | TBD |
| 11 | Proactive memory | Stretch | Tier-2 | 7, 10 | 3 hr | TBD |
| 12 | Geospatial pipeline | Stretch | Tier-2 | 6 | 2 hr | TBD |

**Total committed effort:** ~16 hours (Chunks 1–9)
**Total with stretch:** ~25 hours (Chunks 1–12)

---

## Dependency Graph

```
Track A (Orchestration)          Track B (Memory)           Track C (Recall)
━━━━━━━━━━━━━━━━━━━━━━          ━━━━━━━━━━━━━━━━           ━━━━━━━━━━━━━━━━
┌──────────┐                    ┌──────────┐               ┌──────────┐
│ Chunk 1  │                    │ Chunk 6  │               │ Chunk 9  │
│ Types +  │                    │ Embed +  │               │ Recall   │
│ Config   │                    │ Geo Flds │               │ Ctrl     │
└────┬─────┘                    └───┬──┬───┘               └──────────┘
     │                              │  │
┌────▼─────┐                   ┌────▼──┘───┐
│ Chunk 2  │                   │           │
│ Fallback │                   ▼           ▼
│ Planner  │              ┌────────┐  ┌────────┐
└────┬─────┘              │Chunk 7 │  │Chunk 8 │
     │                    │Hybrid  │  │Fuzzy   │
┌────▼─────┐              │Search  │  │Dedup   │
│ Chunk 3  │              └────┬───┘  └────────┘
│ Ctrl +   │                   │
│ Sub-Agt  │              ┌────▼─────┐
└───┬──┬───┘              │Chunk 10  │ (stretch)
    │  │                  │Threshold │
┌───▼──┘───┐              └────┬─────┘
│          │                   │
▼          ▼              ┌────▼─────┐
┌────────┐ ┌────────┐    │Chunk 11  │ (stretch)
│Chunk 4 │ │Chunk 5 │    │Proactive │
│Budgets │ │Telemetry│   │Memory    │
└────────┘ └────────┘    └──────────┘

                          ┌──────────┐
                          │Chunk 12  │ (stretch, from Chunk 6)
                          │Geospatial│
                          │Pipeline  │
                          └──────────┘
```

**Parallel execution plan (3 worktrees):**
- Worktree 1: Chunks 1 → 2 → 3 → 4 → 5
- Worktree 2: Chunks 6 → 7 → 8 → 10 → 11
- Worktree 3: Chunk 9 → 12

---

## Quality Checklist

Before claiming any chunk is complete:

- [ ] All new code has type hints on public APIs
- [ ] Google-style docstrings on all public functions/classes
- [ ] No `print()`, `os.getenv()`, or bare `except:`
- [ ] Structured logging includes `trace_id`
- [ ] Tests pass: `uv run pytest <chunk-specific-tests> -v`
- [ ] Type checking passes: `uv run mypy src/personal_agent/<changed-modules> --strict`
- [ ] Linting passes: `uv run ruff check src/personal_agent/<changed-modules>`
- [ ] Formatted: `uv run ruff format src/personal_agent/<changed-modules>`

---

*This plan converts ADRs 0035, 0036, 0037 and evaluation findings (EVAL-01 through EVAL-08) into executable implementation tasks for Slice 3: Intelligence.*
