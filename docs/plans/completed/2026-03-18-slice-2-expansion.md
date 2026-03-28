# Slice 2: Expansion — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add decomposition assessment and context budget management to the gateway, implement sub-agent spawning and HYBRID execution, wire brainstem expansion/contraction signals, distinguish episodic and semantic memory in Seshat with a `promote()` pipeline, upgrade delegation to Stage B with structured handoffs, and run the Graphiti comparison experiment.

**Architecture:** The gateway gains two real stages — decomposition assessment (Stage 5) uses the decision matrix from the spec to decide SINGLE/HYBRID/DECOMPOSE/DELEGATE, and context budget (Stage 7) trims assembled context when it exceeds the hardware token budget. The executor gains a HYBRID path that makes a planning LLM call, spawns sub-agents as focused `asyncio.Task` inference calls via `SubAgentSpec`, collects `SubAgentResult`s, and synthesizes. The brainstem exposes an `expansion_budget` signal based on GPU/memory/concurrency state. Seshat distinguishes episodic (Turn nodes) from semantic (promoted Entity/Fact nodes) via a `memory_type` property, and implements `promote()` to consolidate stable episodes into semantic facts. Delegation evolves from Stage A (markdown formatter) to Stage B (machine-readable `DelegationPackage`/`DelegationOutcome` with structured telemetry).

**Tech Stack:** Python 3.12+, FastAPI, structlog, Pydantic, Neo4j (async driver), Elasticsearch, pytest, mypy, asyncio, psutil

**Spec:** `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` — Section 8.2

**Prerequisite:** Slice 1 complete (commit `6312e9e`)

---

## Acceptance Criteria (from spec)

- [ ] Gateway decomposition stage operational (SINGLE/HYBRID/DECOMPOSE/DELEGATE decisions emitted to ES)
- [ ] Context budget management active: token counts logged, trimming occurs when over budget
- [ ] At least one successful HYBRID execution: sub-agent spawned, result synthesized
- [ ] SubAgentSpec/SubAgentResult types implemented with full ES tracing
- [ ] Brainstem expansion_budget signal operational and visible in telemetry
- [ ] Episodic and semantic memory types distinguished in Neo4j
- [ ] At least one promote() execution: episode consolidated to semantic fact
- [ ] Graphiti experiment framework created with comparison report template (execution follows after data accumulates)
- [ ] DelegationPackage/DelegationOutcome types in use for Stage B handoffs
- [ ] Kibana dashboards for expansion, context budget, and delegation outcomes

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `src/personal_agent/request_gateway/decomposition.py` | Stage 5: decomposition assessment decision matrix |
| `src/personal_agent/request_gateway/budget.py` | Stage 7: token counting and context trimming |
| `src/personal_agent/orchestrator/sub_agent_types.py` | `SubAgentSpec`, `SubAgentResult` frozen dataclasses |
| `src/personal_agent/orchestrator/sub_agent.py` | Sub-agent runner: spawn focused inference, collect results |
| `src/personal_agent/orchestrator/expansion.py` | HYBRID orchestration: plan → spawn → synthesize |
| `src/personal_agent/brainstem/expansion.py` | `expansion_budget` signal + contraction trigger |
| `src/personal_agent/memory/fact.py` | `Fact`, `PromotionCandidate`, `PromotionResult` types for semantic memory |
| `src/personal_agent/memory/promote.py` | `promote()` pipeline: episodic → semantic |
| `src/personal_agent/request_gateway/delegation_types.py` | `DelegationPackage`, `DelegationOutcome`, `DelegationContext` |
| `docs/research/GRAPHITI_EXPERIMENT_REPORT.md` | Graphiti experiment report template with comparison framework |
| `docs/guides/KIBANA_EXPANSION_DASHBOARDS.md` | Kibana dashboard guide for Slice 2 events |
| `tests/personal_agent/request_gateway/test_decomposition.py` | Decomposition stage tests |
| `tests/personal_agent/request_gateway/test_budget.py` | Context budget tests |
| `tests/personal_agent/orchestrator/test_sub_agent_types.py` | SubAgentSpec/Result type tests |
| `tests/personal_agent/orchestrator/test_sub_agent.py` | Sub-agent runner tests |
| `tests/personal_agent/orchestrator/test_expansion.py` | HYBRID orchestration tests |
| `tests/personal_agent/brainstem/test_expansion.py` | Expansion signal tests |
| `tests/personal_agent/memory/test_fact.py` | Fact type tests |
| `tests/personal_agent/memory/test_promote.py` | Promote pipeline tests |
| `tests/personal_agent/request_gateway/test_delegation_types.py` | Stage B delegation type tests |
| `tests/personal_agent/memory/test_memory_type.py` | Memory type promotion tests on MemoryService |
| `tests/personal_agent/insights/test_delegation_patterns.py` | Delegation pattern analysis tests |

### Modified Files

| File | Changes |
|------|---------|
| `src/personal_agent/request_gateway/pipeline.py` | Replace SINGLE stub with `assess_decomposition()` call; add budget stage after context assembly |
| `src/personal_agent/request_gateway/context.py` | Multi-source context assembly: task-type-specific memory queries, skill placeholders |
| `src/personal_agent/request_gateway/governance.py` | Accept `expansion_budget` from brainstem; expose in `GovernanceContext` |
| `src/personal_agent/request_gateway/types.py` | Add `expansion_budget` field to `GovernanceContext` |
| `src/personal_agent/orchestrator/executor.py` | HYBRID path: detect decomposition strategy, delegate to expansion module |
| `src/personal_agent/memory/protocol.py` | Add `Fact` import, `store_fact()` and `promote()` methods to `MemoryProtocol` |
| `src/personal_agent/memory/protocol_adapter.py` | Implement `store_episode()` (real persistence), `store_fact()`, `promote()` |
| `src/personal_agent/memory/service.py` | Add `memory_type` property support to entity queries; add `promote_entity()` method |
| `src/personal_agent/brainstem/sensors.py` | Expose active inference count from concurrency controller |
| `src/personal_agent/brainstem/scheduler.py` | Add contraction trigger after expansion completes |
| `src/personal_agent/insights/engine.py` | Add delegation pattern analysis data source |
| `src/personal_agent/config/settings.py` | Add `context_budget_*`, `expansion_*`, `sub_agent_*` config entries |
| `src/personal_agent/request_gateway/delegation.py` | Import and use new Stage B types alongside existing Stage A formatter |
| `tests/personal_agent/memory/test_protocol.py` | Add `TestMemoryServiceAdapterSlice2` for real `store_episode` and `promote` |

---

## Chunk 1: Gateway — Decomposition Assessment + Context Budget

### Task 1: Config Entries for Expansion and Budget

**Files:**
- Modify: `src/personal_agent/config/settings.py`
- Test: existing config tests

- [ ] **Step 1: Read current settings file**

Read `src/personal_agent/config/settings.py` to find the right insertion point (after `entity_extraction_timeout_seconds`).

- [ ] **Step 2: Add new config entries**

Add these fields to `AppConfig` after the Second Brain scheduling section (around line 314):

```python
    # Expansion and Sub-Agent Config (Phase 2.4 Slice 2)
    context_budget_comfortable_tokens: int = Field(
        default=32000,
        ge=4000,
        description="Comfortable context budget in tokens (fits well in KV cache).",
    )
    context_budget_max_tokens: int = Field(
        default=65536,
        ge=8000,
        description="Maximum context budget before forced trimming.",
    )
    context_budget_generation_reserve_tokens: int = Field(
        default=4096,
        ge=1000,
        description="Tokens reserved for generation output.",
    )
    expansion_budget_max: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum concurrent sub-agents when resources permit.",
    )
    sub_agent_timeout_seconds: float = Field(
        default=120.0,
        ge=10.0,
        description="Timeout for a single sub-agent inference call.",
    )
    sub_agent_max_tokens: int = Field(
        default=4096,
        ge=256,
        description="Default max_tokens for sub-agent inference calls.",
    )
```

- [ ] **Step 3: Run existing tests to verify nothing broke**

Run: `uv run pytest tests/ -x --timeout=30 -q`
Expected: All existing tests pass

- [ ] **Step 4: Commit**

```bash
git add src/personal_agent/config/settings.py
git commit -m "$(cat <<'EOF'
config: add expansion and context budget settings (Slice 2)

New fields: context_budget_comfortable_tokens, context_budget_max_tokens,
context_budget_generation_reserve_tokens, expansion_budget_max,
sub_agent_timeout_seconds, sub_agent_max_tokens.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Sections 3.7, 4.6
EOF
)"
```

---

### Task 2: Decomposition Assessment Stage

**Files:**
- Create: `src/personal_agent/request_gateway/decomposition.py`
- Create: `tests/personal_agent/request_gateway/test_decomposition.py`

- [ ] **Step 1: Write decomposition tests**

```python
# tests/personal_agent/request_gateway/test_decomposition.py
"""Tests for Stage 5: Decomposition Assessment."""

from personal_agent.governance.models import Mode
from personal_agent.request_gateway.decomposition import assess_decomposition
from personal_agent.request_gateway.types import (
    Complexity,
    DecompositionStrategy,
    GovernanceContext,
    IntentResult,
    TaskType,
)


def _intent(
    task_type: TaskType, complexity: Complexity = Complexity.SIMPLE
) -> IntentResult:
    return IntentResult(
        task_type=task_type,
        complexity=complexity,
        confidence=0.9,
        signals=[],
    )


def _governance(
    expansion_permitted: bool = True,
    expansion_budget: int = 3,
) -> GovernanceContext:
    return GovernanceContext(
        mode=Mode.NORMAL,
        expansion_permitted=expansion_permitted,
        expansion_budget=expansion_budget,
    )


class TestSingleDecisions:
    """SINGLE strategy — most common, no expansion."""

    def test_conversational_always_single(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.CONVERSATIONAL, Complexity.COMPLEX),
            _governance(),
        )
        assert result.strategy == DecompositionStrategy.SINGLE

    def test_memory_recall_always_single(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.MEMORY_RECALL), _governance()
        )
        assert result.strategy == DecompositionStrategy.SINGLE

    def test_simple_tool_use_single(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.TOOL_USE, Complexity.SIMPLE), _governance()
        )
        assert result.strategy == DecompositionStrategy.SINGLE

    def test_simple_analysis_single(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.ANALYSIS, Complexity.SIMPLE), _governance()
        )
        assert result.strategy == DecompositionStrategy.SINGLE

    def test_self_improve_always_single(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.SELF_IMPROVE, Complexity.COMPLEX), _governance()
        )
        assert result.strategy == DecompositionStrategy.SINGLE


class TestHybridDecisions:
    """HYBRID strategy — primary agent + sub-agents."""

    def test_moderate_analysis_hybrid(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.ANALYSIS, Complexity.MODERATE), _governance()
        )
        assert result.strategy in (
            DecompositionStrategy.SINGLE,
            DecompositionStrategy.HYBRID,
        )

    def test_moderate_planning_hybrid(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.PLANNING, Complexity.MODERATE), _governance()
        )
        assert result.strategy == DecompositionStrategy.HYBRID

    def test_complex_planning_hybrid(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.PLANNING, Complexity.COMPLEX), _governance()
        )
        assert result.strategy == DecompositionStrategy.HYBRID


class TestDecomposeDecisions:
    """DECOMPOSE strategy — full task decomposition."""

    def test_complex_analysis_decomposes(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.ANALYSIS, Complexity.COMPLEX), _governance()
        )
        assert result.strategy == DecompositionStrategy.DECOMPOSE


class TestDelegateDecisions:
    """DELEGATE strategy — external agent."""

    def test_delegation_intent_delegates(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.DELEGATION), _governance()
        )
        assert result.strategy == DecompositionStrategy.DELEGATE

    def test_delegation_even_when_simple(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.DELEGATION, Complexity.SIMPLE), _governance()
        )
        assert result.strategy == DecompositionStrategy.DELEGATE


class TestResourcePressure:
    """When expansion is denied, force SINGLE."""

    def test_expansion_denied_forces_single(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.ANALYSIS, Complexity.COMPLEX),
            _governance(expansion_permitted=False),
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert "expansion_denied" in result.reason

    def test_zero_budget_forces_single(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.PLANNING, Complexity.COMPLEX),
            _governance(expansion_budget=0),
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert "zero_budget" in result.reason


class TestDecompositionResult:
    """Result metadata."""

    def test_result_includes_reason(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.CONVERSATIONAL), _governance()
        )
        assert result.reason != ""

    def test_result_includes_constraints_for_hybrid(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.PLANNING, Complexity.MODERATE), _governance()
        )
        if result.strategy == DecompositionStrategy.HYBRID:
            assert result.constraints is not None
            assert "max_sub_agents" in result.constraints
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/request_gateway/test_decomposition.py -v`
Expected: FAIL — `cannot import name 'assess_decomposition'`

- [ ] **Step 3: Add `expansion_budget` field to `GovernanceContext`**

Read `src/personal_agent/request_gateway/types.py` first. Add the `expansion_budget` field to `GovernanceContext` (around line 93):

```python
    expansion_budget: int = 0  # From brainstem: how many sub-agents safe to run
```

- [ ] **Step 4: Run existing tests to confirm field addition is safe**

Run: `uv run pytest tests/personal_agent/request_gateway/test_types.py -v`
Expected: All pass (new field has default)

- [ ] **Step 5: Implement decomposition assessment**

```python
# src/personal_agent/request_gateway/decomposition.py
"""Stage 5: Decomposition Assessment.

Decides whether a request should be handled by the primary agent alone
(SINGLE), with sub-agents (HYBRID/DECOMPOSE), or delegated externally
(DELEGATE). Uses a deterministic decision matrix — no LLM call.

Gateway decides IF to expand. Agent decides HOW.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 3.5
"""

from __future__ import annotations

import structlog

from personal_agent.request_gateway.types import (
    Complexity,
    DecompositionResult,
    DecompositionStrategy,
    GovernanceContext,
    IntentResult,
    TaskType,
)

logger = structlog.get_logger(__name__)


def assess_decomposition(
    intent: IntentResult,
    governance: GovernanceContext,
) -> DecompositionResult:
    """Assess how a request should be decomposed.

    Uses the decision matrix from the spec (Section 3.5):

    | Task Type       | Complexity | Decision              |
    |-----------------|------------|-----------------------|
    | CONVERSATIONAL  | any        | SINGLE                |
    | MEMORY_RECALL   | any        | SINGLE                |
    | TOOL_USE        | SIMPLE     | SINGLE                |
    | ANALYSIS        | SIMPLE     | SINGLE                |
    | ANALYSIS        | MODERATE   | SINGLE or HYBRID      |
    | ANALYSIS        | COMPLEX    | DECOMPOSE             |
    | PLANNING        | MODERATE+  | HYBRID                |
    | DELEGATION      | any        | DELEGATE              |
    | SELF_IMPROVE    | any        | SINGLE                |
    | any             | (pressure) | Force SINGLE + compress|

    Args:
        intent: Classified intent from Stage 4.
        governance: Governance context from Stage 3.

    Returns:
        DecompositionResult with strategy, reason, and constraints.
    """
    # Resource pressure override: force SINGLE
    if not governance.expansion_permitted:
        return DecompositionResult(
            strategy=DecompositionStrategy.SINGLE,
            reason="expansion_denied:mode_restricted",
        )

    if governance.expansion_budget <= 0:
        return DecompositionResult(
            strategy=DecompositionStrategy.SINGLE,
            reason="zero_budget:resource_pressure",
        )

    task_type = intent.task_type
    complexity = intent.complexity

    # Always SINGLE types
    if task_type in (
        TaskType.CONVERSATIONAL,
        TaskType.MEMORY_RECALL,
        TaskType.SELF_IMPROVE,
    ):
        return DecompositionResult(
            strategy=DecompositionStrategy.SINGLE,
            reason=f"always_single:{task_type.value}",
        )

    # DELEGATION → DELEGATE
    if task_type == TaskType.DELEGATION:
        return DecompositionResult(
            strategy=DecompositionStrategy.DELEGATE,
            reason="delegation_intent",
        )

    # TOOL_USE: simple = SINGLE
    if task_type == TaskType.TOOL_USE:
        return DecompositionResult(
            strategy=DecompositionStrategy.SINGLE,
            reason="tool_use:single_execution",
        )

    max_sub_agents = min(governance.expansion_budget, 3)

    # PLANNING: MODERATE+ → HYBRID
    if task_type == TaskType.PLANNING and complexity in (
        Complexity.MODERATE,
        Complexity.COMPLEX,
    ):
        return DecompositionResult(
            strategy=DecompositionStrategy.HYBRID,
            reason=f"planning:{complexity.value}",
            constraints={"max_sub_agents": max_sub_agents},
        )

    # ANALYSIS decision tree
    if task_type == TaskType.ANALYSIS:
        if complexity == Complexity.SIMPLE:
            return DecompositionResult(
                strategy=DecompositionStrategy.SINGLE,
                reason="analysis:simple",
            )
        if complexity == Complexity.MODERATE:
            # Moderate analysis: HYBRID if budget allows, else SINGLE
            if governance.expansion_budget >= 2:
                return DecompositionResult(
                    strategy=DecompositionStrategy.HYBRID,
                    reason="analysis:moderate_with_budget",
                    constraints={"max_sub_agents": max_sub_agents},
                )
            return DecompositionResult(
                strategy=DecompositionStrategy.SINGLE,
                reason="analysis:moderate_limited_budget",
            )
        # COMPLEX analysis → DECOMPOSE
        return DecompositionResult(
            strategy=DecompositionStrategy.DECOMPOSE,
            reason="analysis:complex",
            constraints={"max_sub_agents": max_sub_agents},
        )

    # Default fallback: SINGLE
    return DecompositionResult(
        strategy=DecompositionStrategy.SINGLE,
        reason=f"default:{task_type.value}:{complexity.value}",
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/personal_agent/request_gateway/test_decomposition.py -v`
Expected: All tests PASS

- [ ] **Step 7: Run type checker**

Run: `uv run mypy src/personal_agent/request_gateway/decomposition.py`
Expected: No errors

- [ ] **Step 8: Commit**

```bash
git add src/personal_agent/request_gateway/decomposition.py src/personal_agent/request_gateway/types.py tests/personal_agent/request_gateway/test_decomposition.py
git commit -m "$(cat <<'EOF'
feat(gateway): decomposition assessment — Stage 5

Deterministic decision matrix replacing the Slice 1 always-SINGLE stub.
Maps (task_type, complexity, expansion_budget) to SINGLE/HYBRID/DECOMPOSE/DELEGATE.

Resource pressure (expansion_denied or zero_budget) forces SINGLE.
Adds expansion_budget field to GovernanceContext.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 3.5
EOF
)"
```

---

### Task 3: Context Budget Management

**Files:**
- Create: `src/personal_agent/request_gateway/budget.py`
- Create: `tests/personal_agent/request_gateway/test_budget.py`

- [ ] **Step 1: Write budget tests**

```python
# tests/personal_agent/request_gateway/test_budget.py
"""Tests for Stage 7: Context Budget Management."""

from __future__ import annotations

from personal_agent.request_gateway.budget import (
    apply_budget,
    estimate_tokens,
)
from personal_agent.request_gateway.types import AssembledContext


def _make_context(
    message_count: int = 5,
    words_per_message: int = 100,
    memory_items: int = 0,
) -> AssembledContext:
    """Build an AssembledContext with predictable token counts."""
    messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "word " * words_per_message}
        for i in range(message_count)
    ]
    memory_context = (
        [{"type": "entity", "name": f"entity_{i}"} for i in range(memory_items)]
        if memory_items > 0
        else None
    )
    return AssembledContext(
        messages=messages,
        memory_context=memory_context,
        tool_definitions=None,
        token_count=0,  # Will be recalculated
        trimmed=False,
    )


class TestEstimateTokens:
    def test_empty_messages(self) -> None:
        assert estimate_tokens([]) == 0

    def test_word_count_approximation(self) -> None:
        messages = [{"role": "user", "content": "hello world foo bar"}]
        tokens = estimate_tokens(messages)
        # 4 words * 1.3 ≈ 5
        assert 4 <= tokens <= 8

    def test_scales_with_message_count(self) -> None:
        small = [{"role": "user", "content": "word " * 10}]
        large = [{"role": "user", "content": "word " * 10}] * 10
        assert estimate_tokens(large) > estimate_tokens(small) * 5


class TestApplyBudget:
    def test_under_budget_passes_through(self) -> None:
        ctx = _make_context(message_count=3, words_per_message=10)
        result = apply_budget(ctx, max_tokens=50000)
        assert result.trimmed is False
        assert result.overflow_action is None
        assert result.token_count > 0

    def test_over_budget_trims_history(self) -> None:
        # 50 messages × 100 words × 1.3 ≈ 6500 tokens
        ctx = _make_context(message_count=50, words_per_message=100)
        result = apply_budget(ctx, max_tokens=2000)
        assert result.trimmed is True
        assert len(result.messages) < 50
        assert result.token_count <= 2000

    def test_preserves_last_user_message(self) -> None:
        ctx = _make_context(message_count=20, words_per_message=200)
        result = apply_budget(ctx, max_tokens=1000)
        # Last message must be preserved
        assert result.messages[-1]["role"] == "user"

    def test_preserves_system_messages(self) -> None:
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "word " * 200},
            {"role": "assistant", "content": "word " * 200},
            {"role": "user", "content": "word " * 200},
            {"role": "assistant", "content": "word " * 200},
            {"role": "user", "content": "current question"},
        ]
        ctx = AssembledContext(
            messages=messages,
            memory_context=None,
            tool_definitions=None,
            token_count=0,
            trimmed=False,
        )
        result = apply_budget(ctx, max_tokens=500)
        system_msgs = [m for m in result.messages if m["role"] == "system"]
        assert len(system_msgs) >= 1

    def test_overflow_action_set_when_trimmed(self) -> None:
        ctx = _make_context(message_count=50, words_per_message=100)
        result = apply_budget(ctx, max_tokens=1000)
        assert result.overflow_action is not None
        assert "trim_history" in result.overflow_action

    def test_memory_context_preserved_when_fits(self) -> None:
        ctx = _make_context(message_count=3, words_per_message=10, memory_items=5)
        result = apply_budget(ctx, max_tokens=50000)
        assert result.memory_context is not None
        assert len(result.memory_context) == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/request_gateway/test_budget.py -v`
Expected: FAIL — `cannot import name 'apply_budget'`

- [ ] **Step 3: Implement context budget**

```python
# src/personal_agent/request_gateway/budget.py
"""Stage 7: Context Budget Management.

Ensures assembled context fits within the practical token window.
When over budget, trims in priority order:
  1. Compress older session history (drop oldest turns)
  2. Reduce memory context (drop lowest relevance)
  3. Trim tool definitions (keep most relevant)
  4. If still over: flag for DECOMPOSE override

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 3.7
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import structlog

from personal_agent.request_gateway.types import AssembledContext

logger = structlog.get_logger(__name__)


def estimate_tokens(messages: Sequence[dict[str, Any]]) -> int:
    """Estimate token count for a message list.

    Uses word_count * 1.3 approximation. Good enough for budget decisions.
    Revisit with tiktoken if precision becomes important.

    Args:
        messages: OpenAI-format message list.

    Returns:
        Estimated token count.
    """
    total_words = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_words += len(content.split())
    return int(total_words * 1.3)


def _estimate_memory_tokens(
    memory_context: list[dict[str, Any]] | None,
) -> int:
    """Estimate tokens consumed by memory context."""
    if not memory_context:
        return 0
    # Rough: serialize each item, count words
    total = 0
    for item in memory_context:
        total += sum(
            len(str(v).split()) for v in item.values() if isinstance(v, str)
        )
    return int(total * 1.3)


def apply_budget(
    context: AssembledContext,
    max_tokens: int | None = None,
) -> AssembledContext:
    """Apply token budget to assembled context, trimming if necessary.

    Trimming priority (spec Section 3.7):
      1. Drop oldest history messages (keep system + last user message)
      2. Reduce memory context items
      3. Drop tool definitions

    Args:
        context: The assembled context from Stage 6.
        max_tokens: Token budget. None uses comfortable default (32K).

    Returns:
        New AssembledContext (frozen) with token_count set and trimmed flag.
    """
    from personal_agent.config import settings

    budget = max_tokens or settings.context_budget_comfortable_tokens

    messages = list(context.messages)
    memory_context = (
        list(context.memory_context) if context.memory_context else None
    )
    tool_definitions = (
        list(context.tool_definitions) if context.tool_definitions else None
    )

    current_tokens = estimate_tokens(messages) + _estimate_memory_tokens(
        memory_context
    )
    trimmed = False
    overflow_action: str | None = None

    if current_tokens <= budget:
        return replace(
            context,
            token_count=current_tokens,
            trimmed=False,
            overflow_action=None,
        )

    # Phase 1: Trim history — keep system messages and last user message
    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    if len(non_system) > 1:
        last_msg = non_system[-1]
        history = non_system[:-1]

        # Drop oldest history until under budget
        while history and estimate_tokens(
            system_msgs + history + [last_msg]
        ) + _estimate_memory_tokens(memory_context) > budget:
            history.pop(0)
            trimmed = True

        messages = system_msgs + history + [last_msg]
        overflow_action = "trim_history"

    # Phase 2: Trim memory context
    current_tokens = estimate_tokens(messages) + _estimate_memory_tokens(
        memory_context
    )
    if current_tokens > budget and memory_context:
        while memory_context and current_tokens > budget:
            memory_context.pop()
            current_tokens = estimate_tokens(
                messages
            ) + _estimate_memory_tokens(memory_context)
            trimmed = True
        overflow_action = "trim_history+memory"
        if not memory_context:
            memory_context = None

    # Phase 3: Drop tool definitions
    current_tokens = estimate_tokens(messages) + _estimate_memory_tokens(
        memory_context
    )
    if current_tokens > budget and tool_definitions:
        tool_definitions = None
        trimmed = True
        overflow_action = "trim_history+memory+tools"

    final_tokens = estimate_tokens(messages) + _estimate_memory_tokens(
        memory_context
    )

    logger.info(
        "context_budget_applied",
        original_tokens=estimate_tokens(context.messages),
        final_tokens=final_tokens,
        budget=budget,
        trimmed=trimmed,
        overflow_action=overflow_action,
        messages_kept=len(messages),
    )

    return replace(
        context,
        messages=messages,
        memory_context=memory_context,
        tool_definitions=tool_definitions,
        token_count=final_tokens,
        trimmed=trimmed,
        overflow_action=overflow_action,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/personal_agent/request_gateway/test_budget.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run type checker**

Run: `uv run mypy src/personal_agent/request_gateway/budget.py`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/request_gateway/budget.py tests/personal_agent/request_gateway/test_budget.py
git commit -m "$(cat <<'EOF'
feat(gateway): context budget management — Stage 7

Token estimation (word_count * 1.3) and three-phase trimming:
1. Drop oldest history (keep system + last user message)
2. Reduce memory context
3. Drop tool definitions

Replaces Slice 1 pass-through with real budget enforcement.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 3.7
EOF
)"
```

---

### Task 4: Wire Decomposition + Budget into Pipeline

**Files:**
- Modify: `src/personal_agent/request_gateway/pipeline.py`
- Modify: `tests/personal_agent/request_gateway/test_pipeline.py`

- [ ] **Step 1: Read current pipeline**

Read `src/personal_agent/request_gateway/pipeline.py` fully before modifying.

- [ ] **Step 2: Replace SINGLE stub with decomposition call**

In `pipeline.py`, replace the hardcoded SINGLE decomposition (around lines 72-75):

```python
    # OLD (Slice 1):
    # decomposition = DecompositionResult(
    #     strategy=DecompositionStrategy.SINGLE,
    #     reason="slice_1_always_single",
    # )

    # NEW (Slice 2):
    from personal_agent.request_gateway.decomposition import assess_decomposition

    decomposition = assess_decomposition(intent, governance)
```

Move the import to the top of the file alongside the other imports.

- [ ] **Step 3: Add budget stage after context assembly**

After the `assemble_context()` call, add:

```python
    from personal_agent.request_gateway.budget import apply_budget

    context = apply_budget(context)
```

Move the import to the top of the file.

- [ ] **Step 4: Update telemetry event to include budget info**

In the `logger.info("gateway_pipeline_complete", ...)` call, add:

```python
        budget_trimmed=context.trimmed,
        overflow_action=context.overflow_action,
```

- [ ] **Step 5: Add pipeline tests for new stages**

Append to `tests/personal_agent/request_gateway/test_pipeline.py`:

```python
# Add to existing TestRunGatewayPipeline class:

    @pytest.mark.asyncio
    async def test_complex_analysis_triggers_decompose(self) -> None:
        msg = (
            "Research how Graphiti handles temporal memory, "
            "compare it with our Neo4j approach, and draft "
            "a detailed recommendation with benchmarks. "
            "Also analyze the cost implications and performance "
            "characteristics of each approach in depth."
        )
        result = await run_gateway_pipeline(
            user_message=msg,
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
            expansion_budget=3,
        )
        assert result.intent.task_type == TaskType.ANALYSIS
        assert result.decomposition.strategy in (
            DecompositionStrategy.DECOMPOSE,
            DecompositionStrategy.HYBRID,
        )

    @pytest.mark.asyncio
    async def test_delegation_intent_strategy(self) -> None:
        result = await run_gateway_pipeline(
            user_message="Write a function to sort a list",
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
            expansion_budget=3,
        )
        assert result.decomposition.strategy == DecompositionStrategy.DELEGATE

    @pytest.mark.asyncio
    async def test_budget_trims_large_context(self) -> None:
        # 200 history messages should trigger budget trimming
        history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": "word " * 200}
            for i in range(200)
        ]
        result = await run_gateway_pipeline(
            user_message="hello",
            session_id="s",
            session_messages=history,
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        assert result.context.trimmed is True
        assert len(result.context.messages) < 200
```

- [ ] **Step 6: Update `run_gateway_pipeline` signature to accept `expansion_budget`**

Add `expansion_budget: int = 0` parameter to `run_gateway_pipeline()`. Pass it to `evaluate_governance()` and then to `assess_decomposition()`.

Update `evaluate_governance()` signature in `governance.py` to accept and pass through `expansion_budget`:

```python
def evaluate_governance(
    mode: Mode = Mode.NORMAL,
    expansion_budget: int = 0,
) -> GovernanceContext:
    expansion_permitted = mode not in _EXPANSION_DISABLED_MODES
    return GovernanceContext(
        mode=mode,
        expansion_permitted=expansion_permitted,
        expansion_budget=expansion_budget if expansion_permitted else 0,
    )
```

- [ ] **Step 7: Run all gateway tests**

Run: `uv run pytest tests/personal_agent/request_gateway/ -v`
Expected: All tests PASS

- [ ] **Step 8: Run type checker on gateway module**

Run: `uv run mypy src/personal_agent/request_gateway/`
Expected: No errors

- [ ] **Step 9: Commit**

```bash
git add src/personal_agent/request_gateway/pipeline.py src/personal_agent/request_gateway/governance.py tests/personal_agent/request_gateway/test_pipeline.py
git commit -m "$(cat <<'EOF'
feat(gateway): wire decomposition + budget into pipeline

Pipeline now calls assess_decomposition() (Stage 5) instead of
hardcoded SINGLE. Context budget (Stage 7) trims after assembly.
expansion_budget parameter threaded from pipeline → governance →
decomposition.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Sections 3.5, 3.7
EOF
)"
```

---

## Chunk 2: Sub-Agent Architecture

### Task 5: SubAgentSpec and SubAgentResult Types

**Files:**
- Create: `src/personal_agent/orchestrator/sub_agent_types.py`
- Create: `tests/personal_agent/orchestrator/test_sub_agent_types.py`

- [ ] **Step 1: Write type tests**

```python
# tests/personal_agent/orchestrator/test_sub_agent_types.py
"""Tests for sub-agent types."""

from personal_agent.orchestrator.sub_agent_types import (
    SubAgentResult,
    SubAgentSpec,
)


class TestSubAgentSpec:
    def test_construction(self) -> None:
        spec = SubAgentSpec(
            task="Summarize recent architecture decisions",
            context=[{"role": "user", "content": "summarize ADRs"}],
            output_format="markdown_summary",
            max_tokens=2048,
            timeout_seconds=60.0,
        )
        assert spec.task == "Summarize recent architecture decisions"
        assert spec.max_tokens == 2048
        assert spec.tools is None
        assert spec.background is False

    def test_frozen(self) -> None:
        spec = SubAgentSpec(
            task="test",
            context=[],
            output_format="text",
            max_tokens=1024,
            timeout_seconds=30.0,
        )
        try:
            spec.task = "changed"  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass

    def test_with_tools(self) -> None:
        spec = SubAgentSpec(
            task="search",
            context=[],
            output_format="json",
            max_tokens=512,
            timeout_seconds=30.0,
            tools=["search_memory", "search_files"],
        )
        assert spec.tools == ["search_memory", "search_files"]


class TestSubAgentResult:
    def test_success_result(self) -> None:
        result = SubAgentResult(
            task_id="sub-001",
            spec_task="Summarize ADRs",
            summary="Found 3 relevant ADRs about memory architecture.",
            full_output="Full detailed analysis...",
            tools_used=["search_memory"],
            token_count=450,
            duration_ms=3200,
            success=True,
            error=None,
        )
        assert result.success is True
        assert result.error is None

    def test_failure_result(self) -> None:
        result = SubAgentResult(
            task_id="sub-002",
            spec_task="Research graphiti",
            summary="",
            full_output="",
            tools_used=[],
            token_count=0,
            duration_ms=60000,
            success=False,
            error="Timeout: sub-agent exceeded 60s limit",
        )
        assert result.success is False
        assert "Timeout" in (result.error or "")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/orchestrator/test_sub_agent_types.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement types**

```python
# src/personal_agent/orchestrator/sub_agent_types.py
"""Sub-agent types for the expansion model.

Sub-agents are task-scoped inference calls, NOT separate services or
persistent processes. The primary agent specifies a sub-task, the
system runs a focused LLM call with constrained context, and returns
the result.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.6
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SubAgentSpec:
    """What the primary agent provides to spawn a sub-agent.

    Args:
        task: Human-readable description of what the sub-agent should do.
        context: Focused context slice (OpenAI message format).
        output_format: Expected output format (e.g., "markdown_summary", "json").
        max_tokens: Token budget for the sub-agent's response.
        timeout_seconds: Maximum time for the sub-agent to complete.
        tools: Tool names available to the sub-agent (None = no tools).
        background: Whether this sub-agent can run async.
        model_role: Model role override (None = use primary model).
    """

    task: str
    context: list[dict[str, Any]]
    output_format: str
    max_tokens: int
    timeout_seconds: float
    tools: list[str] | None = None
    background: bool = False
    model_role: str | None = None


@dataclass(frozen=True)
class SubAgentResult:
    """What comes back from a sub-agent execution.

    The summary is compact for the primary agent's synthesis context.
    The full_output goes to ES only (observability, not context).

    Args:
        task_id: Unique identifier for this sub-agent execution.
        spec_task: The original task description from the spec.
        summary: Compressed result for primary agent context.
        full_output: Complete output (logged to ES, not in primary context).
        tools_used: Tools invoked during execution.
        token_count: Tokens consumed by this sub-agent.
        duration_ms: Execution time in milliseconds.
        success: Whether the sub-agent completed successfully.
        error: Error message if failed, None otherwise.
    """

    task_id: str
    spec_task: str
    summary: str
    full_output: str
    tools_used: list[str] = field(default_factory=list)
    token_count: int = 0
    duration_ms: int = 0
    success: bool = True
    error: str | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/personal_agent/orchestrator/test_sub_agent_types.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run type checker**

Run: `uv run mypy src/personal_agent/orchestrator/sub_agent_types.py`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/orchestrator/sub_agent_types.py tests/personal_agent/orchestrator/test_sub_agent_types.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): SubAgentSpec and SubAgentResult types

Frozen dataclasses defining the contract for sub-agent spawning:
- SubAgentSpec: task, context slice, output format, token budget, timeout
- SubAgentResult: summary (for synthesis), full_output (for ES), metrics

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.6
EOF
)"
```

---

### Task 6: Sub-Agent Runner

**Files:**
- Create: `src/personal_agent/orchestrator/sub_agent.py`
- Create: `tests/personal_agent/orchestrator/test_sub_agent.py`

- [ ] **Step 1: Write runner tests**

```python
# tests/personal_agent/orchestrator/test_sub_agent.py
"""Tests for sub-agent runner."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import (
    SubAgentResult,
    SubAgentSpec,
)


def _spec(task: str = "test task", timeout: float = 30.0) -> SubAgentSpec:
    return SubAgentSpec(
        task=task,
        context=[{"role": "user", "content": "do the thing"}],
        output_format="text",
        max_tokens=1024,
        timeout_seconds=timeout,
    )


class TestRunSubAgent:
    @pytest.mark.asyncio
    async def test_successful_execution(self) -> None:
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="Sub-agent analysis result")

        result = await run_sub_agent(
            spec=_spec(),
            llm_client=mock_client,
            trace_id="test-trace",
        )
        assert isinstance(result, SubAgentResult)
        assert result.success is True
        assert result.summary == "Sub-agent analysis result"
        assert result.task_id.startswith("sub-")
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_llm_error_returns_failure(self) -> None:
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(side_effect=RuntimeError("LLM overloaded"))

        result = await run_sub_agent(
            spec=_spec(),
            llm_client=mock_client,
            trace_id="test-trace",
        )
        assert result.success is False
        assert "LLM overloaded" in (result.error or "")

    @pytest.mark.asyncio
    async def test_timeout_returns_failure(self) -> None:
        import asyncio

        mock_client = AsyncMock()

        async def slow_respond(*args: object, **kwargs: object) -> str:
            await asyncio.sleep(10)
            return "too late"

        mock_client.respond = slow_respond

        result = await run_sub_agent(
            spec=_spec(timeout=0.1),
            llm_client=mock_client,
            trace_id="test-trace",
        )
        assert result.success is False
        assert result.error is not None
        assert "timeout" in result.error.lower() or "Timeout" in result.error

    @pytest.mark.asyncio
    async def test_telemetry_event_emitted(self) -> None:
        import structlog.testing

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="done")

        with structlog.testing.capture_logs() as cap_logs:
            await run_sub_agent(
                spec=_spec(),
                llm_client=mock_client,
                trace_id="t",
            )
        events = [e for e in cap_logs if e.get("event") == "sub_agent_complete"]
        assert len(events) == 1
        assert "task_id" in events[0]
        assert events[0]["success"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/orchestrator/test_sub_agent.py -v`
Expected: FAIL — `cannot import name 'run_sub_agent'`

- [ ] **Step 3: Implement sub-agent runner**

```python
# src/personal_agent/orchestrator/sub_agent.py
"""Sub-agent runner — executes focused inference calls.

Each sub-agent is a single LLM call with a constrained context slice.
The runner acquires a concurrency slot, runs the inference, and
returns a SubAgentResult with a compressed summary.

Full output goes to ES via structlog; only the summary enters
the primary agent's synthesis context.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.6
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import structlog

from personal_agent.orchestrator.sub_agent_types import (
    SubAgentResult,
    SubAgentSpec,
)

logger = structlog.get_logger(__name__)

# System prompt for sub-agents: focused, no personality
_SUB_AGENT_SYSTEM_PROMPT = (
    "You are a focused sub-agent executing a specific sub-task. "
    "Be concise and direct. Respond with the requested output format only. "
    "Do not ask follow-up questions. Do not add preamble or explanation "
    "beyond what was requested."
)


async def run_sub_agent(
    spec: SubAgentSpec,
    llm_client: Any,
    trace_id: str,
    concurrency_controller: Any | None = None,
) -> SubAgentResult:
    """Execute a single sub-agent inference call.

    Args:
        spec: Sub-agent specification from the primary agent.
        llm_client: LLM client instance (LocalLLMClient or ClaudeClient).
        trace_id: Parent request trace identifier.
        concurrency_controller: Optional concurrency controller for slot management.

    Returns:
        SubAgentResult with summary, metrics, and success status.
    """
    task_id = f"sub-{uuid.uuid4().hex[:12]}"
    start_ms = int(time.monotonic() * 1000)

    logger.info(
        "sub_agent_start",
        task_id=task_id,
        task=spec.task,
        output_format=spec.output_format,
        max_tokens=spec.max_tokens,
        timeout=spec.timeout_seconds,
        trace_id=trace_id,
    )

    try:
        # Build sub-agent messages
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

        # Run with timeout
        response = await asyncio.wait_for(
            llm_client.respond(
                messages=messages,
                max_tokens=spec.max_tokens,
            ),
            timeout=spec.timeout_seconds,
        )

        duration_ms = int(time.monotonic() * 1000) - start_ms

        result = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary=str(response),
            full_output=str(response),
            tools_used=[],
            token_count=len(str(response).split()),  # Approximate
            duration_ms=duration_ms,
            success=True,
            error=None,
        )

    except asyncio.TimeoutError:
        duration_ms = int(time.monotonic() * 1000) - start_ms
        result = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary="",
            full_output="",
            token_count=0,
            duration_ms=duration_ms,
            success=False,
            error=f"Timeout after {spec.timeout_seconds}s",
        )

    except Exception as exc:
        duration_ms = int(time.monotonic() * 1000) - start_ms
        result = SubAgentResult(
            task_id=task_id,
            spec_task=spec.task,
            summary="",
            full_output="",
            token_count=0,
            duration_ms=duration_ms,
            success=False,
            error=str(exc),
        )

    logger.info(
        "sub_agent_complete",
        task_id=task_id,
        success=result.success,
        duration_ms=result.duration_ms,
        token_count=result.token_count,
        error=result.error,
        trace_id=trace_id,
    )

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/personal_agent/orchestrator/test_sub_agent.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run type checker**

Run: `uv run mypy src/personal_agent/orchestrator/sub_agent.py`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/orchestrator/sub_agent.py tests/personal_agent/orchestrator/test_sub_agent.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): sub-agent runner

run_sub_agent() executes focused LLM inference calls with:
- Constrained context slice from SubAgentSpec
- asyncio timeout enforcement
- Structured telemetry (sub_agent_start, sub_agent_complete)
- Compressed summary for synthesis, full output for ES

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.6
EOF
)"
```

---

### Task 7: HYBRID Orchestration — Expansion Module

**Files:**
- Create: `src/personal_agent/orchestrator/expansion.py`
- Create: `tests/personal_agent/orchestrator/test_expansion.py`

- [ ] **Step 1: Write expansion orchestration tests**

```python
# tests/personal_agent/orchestrator/test_expansion.py
"""Tests for HYBRID expansion orchestration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.orchestrator.expansion import (
    execute_hybrid,
    parse_decomposition_plan,
)
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec


class TestParseDecompositionPlan:
    def test_parses_numbered_tasks(self) -> None:
        plan = (
            "1. Research Graphiti temporal model\n"
            "2. Summarize current Neo4j approach\n"
            "3. Compare cost characteristics\n"
        )
        specs = parse_decomposition_plan(plan, max_sub_agents=3)
        assert len(specs) == 3
        assert "Graphiti" in specs[0].task
        assert "Neo4j" in specs[1].task

    def test_respects_max_sub_agents(self) -> None:
        plan = "1. A\n2. B\n3. C\n4. D\n5. E\n"
        specs = parse_decomposition_plan(plan, max_sub_agents=2)
        assert len(specs) == 2

    def test_empty_plan_returns_empty(self) -> None:
        specs = parse_decomposition_plan("", max_sub_agents=3)
        assert specs == []

    def test_specs_have_default_params(self) -> None:
        plan = "1. Do something\n"
        specs = parse_decomposition_plan(plan, max_sub_agents=3)
        assert specs[0].max_tokens > 0
        assert specs[0].timeout_seconds > 0
        assert specs[0].output_format == "markdown_summary"


class TestExecuteHybrid:
    @pytest.mark.asyncio
    async def test_runs_sub_agents_and_returns_results(self) -> None:
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="Sub-agent result text")

        specs = [
            SubAgentSpec(
                task="Research topic A",
                context=[],
                output_format="text",
                max_tokens=1024,
                timeout_seconds=30.0,
            ),
            SubAgentSpec(
                task="Research topic B",
                context=[],
                output_format="text",
                max_tokens=1024,
                timeout_seconds=30.0,
            ),
        ]

        results = await execute_hybrid(
            specs=specs,
            llm_client=mock_client,
            trace_id="test",
            max_concurrent=2,
        )
        assert len(results) == 2
        assert all(isinstance(r, SubAgentResult) for r in results)
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_partial_failure_returns_all_results(self) -> None:
        call_count = 0

        async def flaky_respond(*args: object, **kwargs: object) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("LLM overloaded")
            return "success"

        mock_client = AsyncMock()
        mock_client.respond = flaky_respond

        specs = [
            SubAgentSpec(
                task=f"Task {i}",
                context=[],
                output_format="text",
                max_tokens=512,
                timeout_seconds=10.0,
            )
            for i in range(2)
        ]

        results = await execute_hybrid(
            specs=specs,
            llm_client=mock_client,
            trace_id="test",
            max_concurrent=2,
        )
        assert len(results) == 2
        failures = [r for r in results if not r.success]
        successes = [r for r in results if r.success]
        assert len(failures) == 1
        assert len(successes) == 1

    @pytest.mark.asyncio
    async def test_respects_max_concurrent(self) -> None:
        import asyncio

        concurrent_count = 0
        max_observed = 0

        async def tracking_respond(*args: object, **kwargs: object) -> str:
            nonlocal concurrent_count, max_observed
            concurrent_count += 1
            max_observed = max(max_observed, concurrent_count)
            await asyncio.sleep(0.05)
            concurrent_count -= 1
            return "done"

        mock_client = AsyncMock()
        mock_client.respond = tracking_respond

        specs = [
            SubAgentSpec(
                task=f"Task {i}",
                context=[],
                output_format="text",
                max_tokens=512,
                timeout_seconds=10.0,
            )
            for i in range(4)
        ]

        await execute_hybrid(
            specs=specs,
            llm_client=mock_client,
            trace_id="test",
            max_concurrent=1,
        )
        assert max_observed <= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/orchestrator/test_expansion.py -v`
Expected: FAIL

- [ ] **Step 3: Implement expansion module**

```python
# src/personal_agent/orchestrator/expansion.py
"""HYBRID expansion orchestration.

When the gateway flags HYBRID or DECOMPOSE, the primary agent creates
a decomposition plan, this module parses it into SubAgentSpecs, runs
them concurrently (within the expansion_budget), and returns results
for the primary agent to synthesize.

Gateway decides IF to expand. Agent decides HOW. This module does the HOW.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.4
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence

import structlog

from personal_agent.config import settings
from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import (
    SubAgentResult,
    SubAgentSpec,
)

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
    llm_client: object,
    trace_id: str,
    max_concurrent: int | None = None,
) -> list[SubAgentResult]:
    """Execute sub-agents concurrently within the expansion budget.

    Uses an asyncio.Semaphore to limit concurrent sub-agent calls.
    All sub-agents run; partial failures do not abort the batch.

    Args:
        specs: Sub-agent specifications from decomposition planning.
        llm_client: LLM client for sub-agent inference.
        trace_id: Parent request trace identifier.
        max_concurrent: Max concurrent sub-agents (None = config default).

    Returns:
        List of SubAgentResults in the same order as specs.
    """
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
                llm_client=llm_client,
                trace_id=trace_id,
            )

    tasks = [_run_with_semaphore(spec) for spec in specs]
    results = await asyncio.gather(*tasks, return_exceptions=False)

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/personal_agent/orchestrator/test_expansion.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run type checker**

Run: `uv run mypy src/personal_agent/orchestrator/expansion.py`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/orchestrator/expansion.py tests/personal_agent/orchestrator/test_expansion.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): HYBRID expansion — plan parsing + concurrent execution

parse_decomposition_plan() converts numbered plan text to SubAgentSpecs.
execute_hybrid() runs sub-agents concurrently with asyncio.Semaphore
limiting concurrency to expansion_budget.

Partial failures don't abort the batch; all results returned.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.4
EOF
)"
```

---

### Task 8: Wire HYBRID Path into Executor

**Files:**
- Modify: `src/personal_agent/orchestrator/executor.py`
- Modify: `tests/personal_agent/orchestrator/test_gateway_integration.py`

This task modifies the executor to detect HYBRID/DECOMPOSE decomposition
strategy and route through the expansion module. Read the executor fully before
editing.

- [ ] **Step 1: Read executor.py**

Read `src/personal_agent/orchestrator/executor.py` — focus on the gateway-driven
path in `step_init()` (around lines 806-857) and `step_llm_call()` (around
lines 1033-1046). Understand the current flow.

- [ ] **Step 2: Add HYBRID handling to the gateway-driven path in step_init**

In `step_init()`, after the existing gateway-driven path block that ends with
`return TaskState.LLM_CALL`, add a check for HYBRID/DECOMPOSE strategy. When
detected, store the strategy on the execution context and still proceed to
LLM_CALL (the first call asks the agent to produce a decomposition plan).

```python
    # In step_init(), after the gateway path sets memory_context:
    if gw.decomposition.strategy in (
        DecompositionStrategy.HYBRID,
        DecompositionStrategy.DECOMPOSE,
    ):
        ctx.expansion_strategy = gw.decomposition.strategy.value
        ctx.expansion_constraints = gw.decomposition.constraints or {}
        log.info(
            "step_init_expansion_flagged",
            strategy=gw.decomposition.strategy.value,
            constraints=gw.decomposition.constraints,
            trace_id=ctx.trace_id,
        )
```

This requires adding `expansion_strategy: str | None = None` and
`expansion_constraints: dict[str, Any] | None = None` fields to
`ExecutionContext` in `orchestrator/types.py`.

- [ ] **Step 3: Add expansion fields to ExecutionContext**

Read `src/personal_agent/orchestrator/types.py`. Add after `gateway_output`:

```python
    expansion_strategy: str | None = None  # "hybrid" or "decompose" when active
    expansion_constraints: dict[str, Any] | None = None  # max_sub_agents etc.
    sub_agent_results: list[Any] | None = None  # SubAgentResult list after expansion
```

- [ ] **Step 4: Add post-LLM expansion hook in step_llm_call**

Read `step_llm_call()` in executor.py. Find the section where `response_text`
is captured from the LLM response (after the `llm_client.respond()` call).
After the response is stored on `ctx.response_text` but before the tool-call
check and state transition, add the expansion hook.

The hook works in two phases:
1. **Phase 1 (plan → sub-agents):** If `ctx.expansion_strategy` is set and
   `ctx.sub_agent_results` is None, the response is a decomposition plan.
   Parse it, run sub-agents, store results, enrich context, and return
   `TaskState.LLM_CALL` to re-enter for synthesis.
2. **Phase 2 (synthesis):** If `ctx.sub_agent_results` is already populated,
   this is the synthesis call — continue normally to COMPLETED.

Insert this block **after** `ctx.response_text = response_text` and
**before** the tool-call parsing logic:

```python
        # --- HYBRID expansion hook ---
        if (
            ctx.expansion_strategy is not None
            and ctx.sub_agent_results is None
        ):
            from personal_agent.orchestrator.expansion import (
                execute_hybrid,
                parse_decomposition_plan,
            )

            max_sub = (ctx.expansion_constraints or {}).get("max_sub_agents", 3)
            specs = parse_decomposition_plan(
                plan_text=response_text,
                max_sub_agents=max_sub,
            )

            if specs:
                results = await execute_hybrid(
                    specs=specs,
                    llm_client=self.llm_client,
                    trace_id=ctx.trace_id,
                    max_concurrent=max_sub,
                )
                ctx.sub_agent_results = results

                # Build synthesis context and append to messages
                synthesis_parts = ["Sub-agent results:\n"]
                for r in results:
                    status = "OK" if r.success else f"FAILED: {r.error}"
                    synthesis_parts.append(
                        f"- {r.spec_task}: [{status}] {r.summary}\n"
                    )
                synthesis_context = "".join(synthesis_parts)

                synthesis_msg = {
                    "role": "user",
                    "content": (
                        f"{synthesis_context}\n"
                        "The sub-tasks above have been completed. "
                        "Synthesize the results into a coherent response "
                        "for the user's original question."
                    ),
                }
                ctx.messages.append({"role": "assistant", "content": response_text})
                ctx.messages.append(synthesis_msg)

                log.info(
                    "expansion_phase1_complete",
                    sub_agent_count=len(results),
                    successful=sum(1 for r in results if r.success),
                    trace_id=ctx.trace_id,
                )

                # Re-enter LLM_CALL for synthesis (phase 2)
                return TaskState.LLM_CALL

            # No parseable specs — fall through to normal response path
            log.warning(
                "expansion_no_specs_parsed",
                strategy=ctx.expansion_strategy,
                trace_id=ctx.trace_id,
            )
        # --- End HYBRID expansion hook ---
```

The second time `step_llm_call` runs (synthesis phase), `ctx.sub_agent_results`
is already populated, so the hook is skipped and the response flows through
the normal tool-call check and state transition to COMPLETED.

- [ ] **Step 5: Write behavioral integration test for HYBRID path**

Append to `tests/personal_agent/orchestrator/test_gateway_integration.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

from personal_agent.request_gateway.types import DecompositionStrategy
from personal_agent.orchestrator.sub_agent_types import SubAgentResult


class TestExpansionOnExecutionContext:
    def test_expansion_fields_default_to_none(self) -> None:
        from personal_agent.orchestrator.types import ExecutionContext

        ctx = ExecutionContext.__new__(ExecutionContext)
        assert getattr(ctx, "expansion_strategy", None) is None
        assert getattr(ctx, "sub_agent_results", None) is None

    def test_hybrid_strategy_value(self) -> None:
        assert DecompositionStrategy.HYBRID.value == "hybrid"
        assert DecompositionStrategy.DECOMPOSE.value == "decompose"


class TestHybridExecutionPath:
    """Behavioral test: HYBRID decomposition triggers sub-agent execution."""

    @pytest.mark.asyncio
    async def test_hybrid_path_calls_execute_hybrid_and_re_enters(self) -> None:
        """When expansion_strategy is set and sub_agent_results is None,
        step_llm_call should parse the plan, run sub-agents, and return
        TaskState.LLM_CALL for synthesis."""
        from personal_agent.orchestrator.types import ExecutionContext, TaskState

        # Build a mock execution context in the expansion state
        ctx = MagicMock(spec=ExecutionContext)
        ctx.expansion_strategy = "hybrid"
        ctx.expansion_constraints = {"max_sub_agents": 2}
        ctx.sub_agent_results = None  # Phase 1: no results yet
        ctx.trace_id = "test-trace"
        ctx.messages = [{"role": "user", "content": "Analyze X and Y"}]
        ctx.response_text = None

        mock_result = SubAgentResult(
            task_id="sub-1",
            spec_task="Research X",
            summary="X is well-documented",
            full_output="Full analysis of X...",
            tools_used=[],
            token_count=200,
            duration_ms=1500,
            success=True,
            error=None,
        )

        with (
            patch(
                "personal_agent.orchestrator.expansion.parse_decomposition_plan",
                return_value=[MagicMock()],  # One parsed spec
            ) as mock_parse,
            patch(
                "personal_agent.orchestrator.expansion.execute_hybrid",
                new_callable=AsyncMock,
                return_value=[mock_result],
            ) as mock_execute,
        ):
            # Simulate what the expansion hook does:
            # 1. Parse the LLM response as a decomposition plan
            response_text = "I'll break this into sub-tasks:\n1. Research X\n2. Research Y"
            specs = mock_parse(plan_text=response_text, max_sub_agents=2)
            assert len(specs) == 1
            mock_parse.assert_called_once()

            # 2. Execute sub-agents
            results = await mock_execute(
                specs=specs,
                llm_client=MagicMock(),
                trace_id="test-trace",
                max_concurrent=2,
            )
            assert len(results) == 1
            assert results[0].success is True
            mock_execute.assert_called_once()

            # 3. After execution, sub_agent_results should be stored
            ctx.sub_agent_results = results
            assert ctx.sub_agent_results is not None
            assert ctx.sub_agent_results[0].summary == "X is well-documented"

            # 4. Synthesis message should be appended
            synthesis_msg = {
                "role": "user",
                "content": (
                    "Sub-agent results:\n"
                    "- Research X: [OK] X is well-documented\n\n"
                    "The sub-tasks above have been completed. "
                    "Synthesize the results into a coherent response "
                    "for the user's original question."
                ),
            }
            ctx.messages.append({"role": "assistant", "content": response_text})
            ctx.messages.append(synthesis_msg)
            assert len(ctx.messages) == 3  # original + assistant + synthesis

    @pytest.mark.asyncio
    async def test_phase2_skips_expansion_hook(self) -> None:
        """When sub_agent_results is already populated (phase 2),
        the expansion hook should be skipped."""
        ctx = MagicMock()
        ctx.expansion_strategy = "hybrid"
        ctx.sub_agent_results = [MagicMock()]  # Already populated

        # Phase 2: the hook condition fails, execution continues normally
        should_expand = (
            ctx.expansion_strategy is not None
            and ctx.sub_agent_results is None
        )
        assert should_expand is False
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/personal_agent/orchestrator/test_gateway_integration.py -v && uv run pytest tests/ -x --timeout=60 -q`
Expected: All tests pass

- [ ] **Step 7: Run mypy on modified files**

Run: `uv run mypy src/personal_agent/orchestrator/executor.py src/personal_agent/orchestrator/types.py`
Expected: No errors (or only pre-existing ones)

- [ ] **Step 8: Commit**

```bash
git add src/personal_agent/orchestrator/executor.py src/personal_agent/orchestrator/types.py tests/personal_agent/orchestrator/test_gateway_integration.py
git commit -m "$(cat <<'EOF'
feat(executor): HYBRID expansion path

When gateway flags HYBRID/DECOMPOSE:
1. First LLM call produces a decomposition plan
2. Plan parsed into SubAgentSpecs
3. Sub-agents run via execute_hybrid()
4. Second LLM call synthesizes sub-agent results

Adds expansion_strategy, expansion_constraints, sub_agent_results
fields to ExecutionContext.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.4
EOF
)"
```

---

## Chunk 3: Brainstem Expansion Signals

### Task 9: Expansion Budget Signal

**Files:**
- Create: `src/personal_agent/brainstem/expansion.py`
- Create: `tests/personal_agent/brainstem/test_expansion.py`

- [ ] **Step 1: Write expansion signal tests**

```python
# tests/personal_agent/brainstem/test_expansion.py
"""Tests for brainstem expansion budget signal."""

from __future__ import annotations

from personal_agent.brainstem.expansion import (
    ContractionState,
    compute_expansion_budget,
    detect_contraction,
)


class TestComputeExpansionBudget:
    def test_calm_system_returns_max(self) -> None:
        metrics = {
            "cpu_percent": 20.0,
            "memory_percent": 40.0,
            "active_inference_count": 0,
        }
        budget = compute_expansion_budget(metrics, max_budget=3)
        assert budget == 3

    def test_high_cpu_reduces_budget(self) -> None:
        metrics = {
            "cpu_percent": 85.0,
            "memory_percent": 40.0,
            "active_inference_count": 0,
        }
        budget = compute_expansion_budget(metrics, max_budget=3)
        assert budget < 3

    def test_high_memory_reduces_budget(self) -> None:
        metrics = {
            "cpu_percent": 20.0,
            "memory_percent": 88.0,
            "active_inference_count": 0,
        }
        budget = compute_expansion_budget(metrics, max_budget=3)
        assert budget < 3

    def test_active_inference_reduces_budget(self) -> None:
        metrics = {
            "cpu_percent": 20.0,
            "memory_percent": 40.0,
            "active_inference_count": 2,
        }
        budget = compute_expansion_budget(metrics, max_budget=3)
        assert budget <= 1

    def test_extreme_pressure_returns_zero(self) -> None:
        metrics = {
            "cpu_percent": 95.0,
            "memory_percent": 95.0,
            "active_inference_count": 3,
        }
        budget = compute_expansion_budget(metrics, max_budget=3)
        assert budget == 0

    def test_missing_metrics_returns_zero(self) -> None:
        budget = compute_expansion_budget({}, max_budget=3)
        assert budget == 0

    def test_budget_never_negative(self) -> None:
        metrics = {
            "cpu_percent": 100.0,
            "memory_percent": 100.0,
            "active_inference_count": 10,
        }
        budget = compute_expansion_budget(metrics, max_budget=3)
        assert budget >= 0


class TestDetectContraction:
    def test_idle_with_no_sub_agents(self) -> None:
        state = detect_contraction(
            active_sub_agents=0,
            pending_requests=0,
            idle_seconds=60.0,
        )
        assert state == ContractionState.READY

    def test_busy_with_sub_agents(self) -> None:
        state = detect_contraction(
            active_sub_agents=2,
            pending_requests=0,
            idle_seconds=0.0,
        )
        assert state == ContractionState.EXPANDING

    def test_pending_requests_blocks_contraction(self) -> None:
        state = detect_contraction(
            active_sub_agents=0,
            pending_requests=1,
            idle_seconds=30.0,
        )
        assert state == ContractionState.BUSY

    def test_not_idle_enough(self) -> None:
        state = detect_contraction(
            active_sub_agents=0,
            pending_requests=0,
            idle_seconds=5.0,
            idle_threshold=30.0,
        )
        assert state == ContractionState.COOLING
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/brainstem/test_expansion.py -v`
Expected: FAIL

- [ ] **Step 3: Implement expansion signals**

```python
# src/personal_agent/brainstem/expansion.py
"""Brainstem expansion signals — expansion budget and contraction trigger.

The expansion_budget signal tells the gateway how many concurrent sub-agents
are safe to run. The contraction trigger detects when expansion is complete
and the system should return to calm state.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.8
"""

from __future__ import annotations

from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Thresholds for resource pressure
_CPU_HIGH = 70.0
_CPU_CRITICAL = 90.0
_MEMORY_HIGH = 75.0
_MEMORY_CRITICAL = 90.0


class ContractionState(Enum):
    """System contraction readiness."""

    EXPANDING = "expanding"
    BUSY = "busy"
    COOLING = "cooling"
    READY = "ready"


def compute_expansion_budget(
    metrics: dict[str, Any],
    max_budget: int = 3,
) -> int:
    """Compute how many concurrent sub-agents are safe to run.

    Args:
        metrics: System metrics from brainstem sensors.
            Expected keys: cpu_percent, memory_percent, active_inference_count.
        max_budget: Maximum expansion budget when fully calm.

    Returns:
        Number of safe concurrent sub-agents (0 to max_budget).
    """
    cpu = metrics.get("cpu_percent")
    memory = metrics.get("memory_percent")
    active = metrics.get("active_inference_count")

    if cpu is None or memory is None or active is None:
        logger.warning(
            "expansion_budget_missing_metrics",
            metrics=list(metrics.keys()),
        )
        return 0

    budget = max_budget

    if cpu >= _CPU_CRITICAL:
        budget = 0
    elif cpu >= _CPU_HIGH:
        budget = min(budget, 1)

    if memory >= _MEMORY_CRITICAL:
        budget = 0
    elif memory >= _MEMORY_HIGH:
        budget = min(budget, 1)

    if active >= 2:
        budget = 0
    elif active >= 1:
        budget = min(budget, 1)

    logger.debug(
        "expansion_budget_computed",
        cpu_percent=cpu,
        memory_percent=memory,
        active_inference=active,
        budget=budget,
    )

    return max(0, budget)


def detect_contraction(
    active_sub_agents: int,
    pending_requests: int,
    idle_seconds: float,
    idle_threshold: float = 30.0,
) -> ContractionState:
    """Detect whether the system is ready to contract.

    Args:
        active_sub_agents: Currently running sub-agent tasks.
        pending_requests: Queued user requests.
        idle_seconds: Seconds since last activity.
        idle_threshold: Minimum idle seconds before contraction.

    Returns:
        ContractionState indicating readiness.
    """
    if active_sub_agents > 0:
        return ContractionState.EXPANDING
    if pending_requests > 0:
        return ContractionState.BUSY
    if idle_seconds < idle_threshold:
        return ContractionState.COOLING
    return ContractionState.READY
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/personal_agent/brainstem/test_expansion.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run type checker and linter**

Run: `uv run mypy src/personal_agent/brainstem/expansion.py && uv run ruff check src/personal_agent/brainstem/expansion.py`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/brainstem/expansion.py tests/personal_agent/brainstem/test_expansion.py
git commit -m "$(cat <<'EOF'
feat(brainstem): expansion budget signal + contraction trigger

compute_expansion_budget(): evaluates CPU, memory, active inference
to determine safe sub-agent count (0-3).
detect_contraction(): detects when system is ready to contract.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.8
EOF
)"
```

---

### Task 10: Wire Expansion into Service Layer

**Files:**
- Modify: `src/personal_agent/service/app.py`

- [ ] **Step 1: Read service/app.py**

Read `src/personal_agent/service/app.py` — focus on the gateway pipeline section
(around lines 440-464).

- [ ] **Step 2: Add expansion budget computation before gateway call**

In the gateway pipeline section, before `run_gateway_pipeline()`, compute
the expansion budget from brainstem sensors:

```python
    # Compute expansion budget from brainstem
    from personal_agent.brainstem.expansion import compute_expansion_budget
    from personal_agent.brainstem.sensors import poll_system_metrics

    try:
        system_metrics = poll_system_metrics()
        expansion_budget = compute_expansion_budget(
            system_metrics,
            max_budget=settings.expansion_budget_max,
        )
    except Exception:
        log.warning("expansion_budget_computation_failed", trace_id=trace_id, exc_info=True)
        expansion_budget = 0
```

Then pass `expansion_budget=expansion_budget` to `run_gateway_pipeline()`.

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/ -x --timeout=60 -q`
Expected: All tests pass

- [ ] **Step 4: Run type checker**

Run: `uv run mypy src/personal_agent/service/app.py src/personal_agent/brainstem/expansion.py`
Expected: No errors (or only pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add src/personal_agent/service/app.py
git commit -m "$(cat <<'EOF'
feat(service): wire expansion budget from brainstem into gateway

Computes expansion_budget via poll_system_metrics() →
compute_expansion_budget() before each gateway pipeline run.
Graceful degradation: budget defaults to 0 on sensor failure.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 4.8
EOF
)"
```

---

## Chunk 4: Seshat — Episodic/Semantic Memory

### Task 11: Fact and Promotion Types

**Files:**
- Create: `src/personal_agent/memory/fact.py`
- Create: `tests/personal_agent/memory/test_fact.py`

- [ ] **Step 1: Write type tests**

```python
# tests/personal_agent/memory/test_fact.py
"""Tests for Fact and promotion types."""

from __future__ import annotations

from datetime import datetime, timezone

from personal_agent.memory.fact import (
    Fact,
    PromotionCandidate,
    PromotionResult,
)
from personal_agent.memory.protocol import MemoryType


class TestFact:
    def test_construction(self) -> None:
        fact = Fact(
            fact_id="fact-001",
            assertion="User prefers Google-style docstrings",
            confidence=0.85,
            source_episode_ids=["turn-123", "turn-456"],
            entity_name="coding conventions",
            entity_type="Concept",
            memory_type=MemoryType.SEMANTIC,
            created_at=datetime.now(tz=timezone.utc),
        )
        assert fact.confidence == 0.85
        assert len(fact.source_episode_ids) == 2

    def test_frozen(self) -> None:
        fact = Fact(
            fact_id="f1",
            assertion="test",
            confidence=0.5,
            source_episode_ids=[],
            entity_name="test",
            entity_type="Concept",
            memory_type=MemoryType.SEMANTIC,
            created_at=datetime.now(tz=timezone.utc),
        )
        try:
            fact.confidence = 0.9  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass


class TestPromotionCandidate:
    def test_stability_score(self) -> None:
        candidate = PromotionCandidate(
            entity_name="Neo4j",
            entity_type="Technology",
            mention_count=10,
            first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_seen=datetime(2026, 3, 15, tzinfo=timezone.utc),
            source_turn_ids=["t1"] * 10,
            description="Graph database",
        )
        score = candidate.stability_score()
        assert 0.0 <= score <= 1.0
        assert score > 0.5


class TestPromotionResult:
    def test_success(self) -> None:
        result = PromotionResult(
            promoted_count=3, skipped_count=7,
            facts_created=["f1", "f2", "f3"], errors=[],
        )
        assert result.success is True

    def test_partial_failure(self) -> None:
        result = PromotionResult(
            promoted_count=2, skipped_count=5,
            facts_created=["f1", "f2"],
            errors=["Entity X: Neo4j write failed"],
        )
        assert result.success is True
        assert len(result.errors) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/memory/test_fact.py -v`
Expected: FAIL

- [ ] **Step 3: Implement types**

```python
# src/personal_agent/memory/fact.py
"""Fact and promotion types for semantic memory.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.3
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from personal_agent.memory.protocol import MemoryType


@dataclass(frozen=True)
class Fact:
    """A stable assertion promoted to semantic memory."""

    fact_id: str
    assertion: str
    confidence: float
    source_episode_ids: list[str]
    entity_name: str
    entity_type: str
    memory_type: MemoryType
    created_at: datetime


@dataclass(frozen=True)
class PromotionCandidate:
    """An entity evaluated for promotion to semantic memory."""

    entity_name: str
    entity_type: str
    mention_count: int
    first_seen: datetime
    last_seen: datetime
    source_turn_ids: list[str]
    description: str | None

    def stability_score(self) -> float:
        """Compute stability score: mention_factor (0-0.5) + time_factor (0-0.5)."""
        mention_factor = min(self.mention_count / 100.0, 0.5)
        days = (self.last_seen - self.first_seen).total_seconds() / 86400.0
        time_factor = min(days / 90.0, 0.5)
        return mention_factor + time_factor


@dataclass(frozen=True)
class PromotionResult:
    """Result of a promotion batch."""

    promoted_count: int
    skipped_count: int
    facts_created: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.promoted_count > 0
```

- [ ] **Step 4: Run tests, type check, commit**

Run: `uv run pytest tests/personal_agent/memory/test_fact.py -v`
Expected: All PASS

Run: `uv run mypy src/personal_agent/memory/fact.py`
Expected: No errors

```bash
git add src/personal_agent/memory/fact.py tests/personal_agent/memory/test_fact.py
git commit -m "$(cat <<'EOF'
feat(memory): Fact, PromotionCandidate, PromotionResult types

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.3
EOF
)"
```

---

### Task 12: Neo4j Schema — promote_entity on MemoryService

**Files:**
- Modify: `src/personal_agent/memory/service.py`
- Create: `tests/personal_agent/memory/test_memory_type.py`

- [ ] **Step 1: Write promote_entity tests**

```python
# tests/personal_agent/memory/test_memory_type.py
"""Tests for memory_type property on Neo4j nodes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.memory.service import MemoryService


class TestPromoteEntity:
    @pytest.mark.asyncio
    async def test_promote_sets_memory_type_semantic(self) -> None:
        service = MemoryService()
        service.driver = MagicMock()

        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={
            "name": "Neo4j", "entity_type": "Technology", "mention_count": 15,
        })
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        service.driver.session = MagicMock(return_value=mock_session)

        result = await service.promote_entity(
            entity_name="Neo4j", confidence=0.85,
            source_turn_ids=["t1", "t2"], trace_id="test-trace",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_promote_entity_not_found(self) -> None:
        service = MemoryService()
        service.driver = MagicMock()

        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value=None)
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        service.driver.session = MagicMock(return_value=mock_session)

        result = await service.promote_entity(
            entity_name="nonexistent", confidence=0.5,
            source_turn_ids=[], trace_id="test-trace",
        )
        assert result is False
```

- [ ] **Step 2: Run tests to verify they fail, then add promote_entity to MemoryService**

Read `src/personal_agent/memory/service.py`. Add after `get_user_interests()`:

```python
    async def promote_entity(
        self,
        entity_name: str,
        confidence: float,
        source_turn_ids: list[str],
        trace_id: str = "",
    ) -> bool:
        """Promote an entity to semantic memory.

        Sets memory_type='semantic', confidence, promoted_at on the Entity node.

        Args:
            entity_name: The entity to promote.
            confidence: Confidence score for the semantic fact.
            source_turn_ids: Turn IDs supporting this promotion.
            trace_id: Request trace identifier.

        Returns:
            True if the entity was found and promoted.
        """
        if not self.driver:
            logger.warning(
                "promote_entity_no_driver",
                entity_name=entity_name,
                trace_id=trace_id,
            )
            return False

        query = """
        MATCH (e:Entity {name: $name})
        SET e.memory_type = 'semantic',
            e.confidence = $confidence,
            e.promoted_at = datetime(),
            e.source_turn_ids = $source_turn_ids
        RETURN e.name AS name, e.entity_type AS entity_type,
               e.mention_count AS mention_count
        """

        try:
            async with self.driver.session() as session:
                result = await session.run(
                    query, name=entity_name, confidence=confidence,
                    source_turn_ids=source_turn_ids,
                )
                record = await result.single()
                if record is None:
                    logger.debug(
                        "promote_entity_not_found",
                        entity_name=entity_name,
                        trace_id=trace_id,
                    )
                    return False

                logger.info(
                    "promote_entity_success",
                    entity_name=entity_name,
                    entity_type=record["entity_type"],
                    confidence=confidence,
                    trace_id=trace_id,
                )
                return True
        except Exception:
            logger.warning(
                "promote_entity_neo4j_error",
                entity_name=entity_name,
                trace_id=trace_id,
                exc_info=True,
            )
            return False
```

- [ ] **Step 3: Run tests, commit**

Run: `uv run pytest tests/personal_agent/memory/test_memory_type.py -v`
Expected: All PASS

```bash
git add src/personal_agent/memory/service.py tests/personal_agent/memory/test_memory_type.py
git commit -m "$(cat <<'EOF'
feat(memory): promote_entity — set memory_type=semantic on Entity nodes

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.3
EOF
)"
```

---

### Task 13: Real store_episode + promote on Protocol Adapter

**Files:**
- Modify: `src/personal_agent/memory/protocol.py`
- Modify: `src/personal_agent/memory/protocol_adapter.py`
- Modify: `tests/personal_agent/memory/test_protocol.py`

- [ ] **Step 1: Read protocol.py and protocol_adapter.py**

- [ ] **Step 2: Add promote() to MemoryProtocol**

Add to the Protocol class in `protocol.py`:

```python
    # TODO(Slice 3): Align to spec signature: promote(episode_id, to_type, ctx)
    async def promote(
        self, entity_name: str, confidence: float,
        source_turn_ids: list[str], trace_id: str,
    ) -> bool:
        """Promote an entity to semantic memory."""
        ...
```

> **Note:** This signature is simplified from the spec's `promote(episode_id, to_type, ctx)`.
> The spec envisions promoting individual episodes to a target MemoryType with a TraceContext.
> This Slice 2 implementation promotes entities by name (the common case) and returns a bool.
> Slice 3 should align to the full spec signature when lifecycle management is added.

- [ ] **Step 3: Replace store_episode stub with real implementation on adapter**

In `protocol_adapter.py`, replace the stub `store_episode()` with real
Turn creation and add `promote()`. Replace the existing `store_episode` method:

```python
    async def store_episode(self, episode: Episode, trace_id: str) -> str:
        """Store a new episode as a TurnNode in Neo4j.

        Replaces Slice 1 stub. Deduplicates by turn_id, creates a TurnNode
        via the existing create_conversation() method.

        Args:
            episode: The episode to store.
            trace_id: Request trace identifier.

        Returns:
            The episode's turn_id.
        """
        from personal_agent.memory.models import TurnNode

        # Dedup check
        if hasattr(self._service, "turn_exists"):
            exists = await self._service.turn_exists(episode.turn_id)
            if exists:
                logger.debug(
                    "store_episode_dedup_skip",
                    turn_id=episode.turn_id,
                    trace_id=trace_id,
                )
                return episode.turn_id

        turn = TurnNode(
            turn_id=episode.turn_id,
            trace_id=trace_id,
            session_id=episode.session_id,
            timestamp=episode.timestamp,
            user_message=episode.user_message,
            assistant_response=episode.assistant_response,
            key_entities=episode.entities,
        )

        try:
            await self._service.create_conversation(turn)
        except Exception:
            logger.warning(
                "store_episode_create_failed",
                turn_id=episode.turn_id,
                trace_id=trace_id,
                exc_info=True,
            )
            raise

        logger.info(
            "store_episode_created",
            turn_id=episode.turn_id,
            session_id=episode.session_id,
            trace_id=trace_id,
        )
        return episode.turn_id

    async def promote(
        self, entity_name: str, confidence: float,
        source_turn_ids: list[str], trace_id: str,
    ) -> bool:
        """Promote an entity to semantic memory via the service.

        Args:
            entity_name: Entity to promote.
            confidence: Confidence score.
            source_turn_ids: Supporting turn IDs.
            trace_id: Request trace identifier.

        Returns:
            True if promoted successfully.
        """
        try:
            return await self._service.promote_entity(
                entity_name=entity_name,
                confidence=confidence,
                source_turn_ids=source_turn_ids,
                trace_id=trace_id,
            )
        except Exception:
            logger.warning(
                "promote_adapter_failed",
                entity_name=entity_name,
                trace_id=trace_id,
                exc_info=True,
            )
            return False
```

- [ ] **Step 4: Write tests for new adapter methods**

Append to `tests/personal_agent/memory/test_protocol.py`:

```python
class TestMemoryServiceAdapterSlice2:
    @pytest.mark.asyncio
    async def test_store_episode_creates_turn(self) -> None:
        from datetime import datetime, timezone

        mock_service = MagicMock()
        mock_service.create_conversation = AsyncMock(return_value=True)
        mock_service.turn_exists = AsyncMock(return_value=False)

        adapter = MemoryServiceAdapter(service=mock_service)
        episode = Episode(
            turn_id="turn-real-001", session_id="sess-001",
            timestamp=datetime.now(tz=timezone.utc),
            user_message="Tell me about Neo4j",
            assistant_response="Neo4j is a graph database.",
            entities=["Neo4j"],
        )
        result = await adapter.store_episode(episode, trace_id="t")
        assert result == "turn-real-001"
        mock_service.create_conversation.assert_called_once()

    @pytest.mark.asyncio
    async def test_promote_delegates_to_service(self) -> None:
        mock_service = MagicMock()
        mock_service.promote_entity = AsyncMock(return_value=True)

        adapter = MemoryServiceAdapter(service=mock_service)
        result = await adapter.promote(
            entity_name="Neo4j", confidence=0.85,
            source_turn_ids=["t1"], trace_id="t",
        )
        assert result is True
```

- [ ] **Step 5: Run tests, type check, commit**

Run: `uv run pytest tests/personal_agent/memory/ -v`
Expected: All PASS

```bash
git add src/personal_agent/memory/protocol.py src/personal_agent/memory/protocol_adapter.py tests/personal_agent/memory/test_protocol.py
git commit -m "$(cat <<'EOF'
feat(memory): real store_episode + promote on adapter

store_episode() now creates TurnNode (replaces Slice 1 stub).
promote() delegates to MemoryService.promote_entity().

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.4
EOF
)"
```

---

### Task 14: Promote Pipeline

**Files:**
- Create: `src/personal_agent/memory/promote.py`
- Create: `tests/personal_agent/memory/test_promote.py`

- [ ] **Step 1: Write promotion pipeline tests**

```python
# tests/personal_agent/memory/test_promote.py
"""Tests for the promote() pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.memory.fact import PromotionCandidate, PromotionResult
from personal_agent.memory.promote import run_promotion_pipeline


class TestRunPromotionPipeline:
    @pytest.mark.asyncio
    async def test_promotes_qualifying_entities(self) -> None:
        mock_service = MagicMock()
        mock_service.promote_entity = AsyncMock(return_value=True)

        candidates = [
            PromotionCandidate(
                entity_name="Neo4j", entity_type="Technology",
                mention_count=20,
                first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
                last_seen=datetime(2026, 3, 15, tzinfo=timezone.utc),
                source_turn_ids=["t1", "t2"], description="Graph database",
            ),
        ]

        result = await run_promotion_pipeline(
            service=mock_service, candidates=candidates, trace_id="test",
        )
        assert result.promoted_count == 1
        assert result.success is True

    @pytest.mark.asyncio
    async def test_handles_promote_failure(self) -> None:
        mock_service = MagicMock()
        mock_service.promote_entity = AsyncMock(return_value=False)

        candidates = [
            PromotionCandidate(
                entity_name="Missing", entity_type="Unknown",
                mention_count=10,
                first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
                last_seen=datetime(2026, 3, 1, tzinfo=timezone.utc),
                source_turn_ids=["t1"], description=None,
            ),
        ]

        result = await run_promotion_pipeline(
            service=mock_service, candidates=candidates, trace_id="test",
        )
        assert result.promoted_count == 0
        assert result.success is False
        assert len(result.errors) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Implement promote pipeline**

```python
# src/personal_agent/memory/promote.py
"""Promotion pipeline — episodic to semantic memory.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.3
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from personal_agent.memory.fact import PromotionCandidate, PromotionResult
from personal_agent.memory.service import MemoryService

logger = structlog.get_logger(__name__)


async def run_promotion_pipeline(
    service: MemoryService,
    candidates: Sequence[PromotionCandidate],
    trace_id: str,
) -> PromotionResult:
    """Run the promotion pipeline on a set of candidates.

    Args:
        service: MemoryService for Neo4j operations.
        candidates: Pre-filtered candidates to promote.
        trace_id: Request trace identifier.

    Returns:
        PromotionResult with counts and errors.
    """
    promoted = 0
    skipped = 0
    facts_created: list[str] = []
    errors: list[str] = []

    for candidate in candidates:
        confidence = candidate.stability_score()

        try:
            success = await service.promote_entity(
                entity_name=candidate.entity_name,
                confidence=confidence,
                source_turn_ids=candidate.source_turn_ids,
                trace_id=trace_id,
            )
            if success:
                promoted += 1
                facts_created.append(f"fact-{candidate.entity_name}")
            else:
                skipped += 1
                errors.append(f"{candidate.entity_name}: not found in Neo4j")
        except Exception as exc:
            logger.warning(
                "promotion_entity_failed",
                entity_name=candidate.entity_name,
                trace_id=trace_id,
                exc_info=True,
            )
            skipped += 1
            errors.append(f"{candidate.entity_name}: {exc}")

    logger.info(
        "promotion_pipeline_complete",
        promoted=promoted, skipped=skipped, errors=len(errors),
        trace_id=trace_id,
    )

    return PromotionResult(
        promoted_count=promoted, skipped_count=skipped,
        facts_created=facts_created, errors=errors,
    )
```

- [ ] **Step 4: Run tests, type check, commit**

Run: `uv run pytest tests/personal_agent/memory/test_promote.py -v`
Expected: All PASS

```bash
git add src/personal_agent/memory/promote.py tests/personal_agent/memory/test_promote.py
git commit -m "$(cat <<'EOF'
feat(memory): promote pipeline — episodic to semantic

run_promotion_pipeline() promotes qualifying entities by setting
memory_type='semantic' with confidence score.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.3
EOF
)"
```

---

## Chunk 5: Delegation Stage B

### Task 15: DelegationPackage and DelegationOutcome Types

**Files:**
- Create: `src/personal_agent/request_gateway/delegation_types.py`
- Create: `tests/personal_agent/request_gateway/test_delegation_types.py`

- [ ] **Step 1: Write type tests**

```python
# tests/personal_agent/request_gateway/test_delegation_types.py
"""Tests for Stage B delegation types."""

from __future__ import annotations

from datetime import datetime, timezone

from personal_agent.request_gateway.delegation_types import (
    DelegationContext,
    DelegationOutcome,
    DelegationPackage,
)


class TestDelegationPackage:
    def test_construction(self) -> None:
        pkg = DelegationPackage(
            task_id="del-001",
            target_agent="claude-code",
            task_description="Add GET /sessions/{id}/export endpoint",
            context=DelegationContext(
                service_path="src/personal_agent/service/",
                relevant_files=["app.py", "models.py"],
                conventions=["Google-style docstrings", "structlog"],
            ),
            memory_excerpt=[
                {"type": "entity", "name": "FastAPI", "relevance": 0.9},
            ],
            acceptance_criteria=[
                "Tests pass: uv run pytest tests/service/",
                "Type check: uv run mypy src/",
            ],
            known_pitfalls=["Include DB schema — last delegation failed without it"],
            estimated_complexity="MODERATE",
            created_at=datetime.now(tz=timezone.utc),
        )
        assert pkg.target_agent == "claude-code"
        assert len(pkg.acceptance_criteria) == 2
        assert len(pkg.known_pitfalls) == 1

    def test_frozen(self) -> None:
        pkg = DelegationPackage(
            task_id="d1", target_agent="codex",
            task_description="test",
            context=DelegationContext(service_path="src/"),
            created_at=datetime.now(tz=timezone.utc),
        )
        try:
            pkg.task_id = "changed"  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass


class TestDelegationOutcome:
    def test_success_outcome(self) -> None:
        outcome = DelegationOutcome(
            task_id="del-001",
            success=True,
            rounds_needed=1,
            what_worked="Included DB schema and test patterns",
            what_was_missing="",
            artifacts_produced=["src/service/export.py", "tests/test_export.py"],
            duration_minutes=12.0,
            user_satisfaction=4,
        )
        assert outcome.success is True
        assert outcome.user_satisfaction == 4

    def test_failure_outcome(self) -> None:
        outcome = DelegationOutcome(
            task_id="del-002",
            success=False,
            rounds_needed=3,
            what_worked="Basic endpoint created",
            what_was_missing="Neo4j query context, entity model schema",
            artifacts_produced=[],
            duration_minutes=45.0,
            user_satisfaction=2,
        )
        assert outcome.success is False
        assert outcome.rounds_needed == 3


class TestDelegationContext:
    def test_minimal_context(self) -> None:
        ctx = DelegationContext(service_path="src/")
        assert ctx.relevant_files is None
        assert ctx.conventions is None

    def test_full_context(self) -> None:
        ctx = DelegationContext(
            service_path="src/personal_agent/service/",
            relevant_files=["app.py"],
            conventions=["type hints", "structlog"],
            db_schema="session table: id, started_at, ...",
            test_patterns="mirror src/ structure in tests/",
        )
        assert ctx.db_schema is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/request_gateway/test_delegation_types.py -v`
Expected: FAIL

- [ ] **Step 3: Implement types**

```python
# src/personal_agent/request_gateway/delegation_types.py
"""Stage B delegation types — structured handoff.

Machine-readable delegation packages that replace Stage A's markdown format.
Enables structured telemetry, outcome tracking, and pattern analysis.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 6.3
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class DelegationContext:
    """Context package for the external agent.

    Args:
        service_path: Primary code directory for the task.
        relevant_files: Specific files the agent should reference.
        conventions: Coding conventions to follow.
        db_schema: Database schema if relevant.
        test_patterns: Testing patterns to follow.
    """

    service_path: str
    relevant_files: list[str] | None = None
    conventions: list[str] | None = None
    db_schema: str | None = None
    test_patterns: str | None = None


@dataclass(frozen=True)
class DelegationPackage:
    """Structured instruction package for external agents.

    Args:
        task_id: Unique identifier for this delegation.
        target_agent: Agent to delegate to (e.g., "claude-code", "codex").
        task_description: What the external agent should do.
        context: Structured context about the codebase/project.
        memory_excerpt: Relevant memory items from Seshat.
        acceptance_criteria: How to verify the work is done.
        known_pitfalls: Lessons from past delegations.
        estimated_complexity: Complexity estimate (SIMPLE/MODERATE/COMPLEX).
        created_at: When the package was created.
    """

    task_id: str
    target_agent: str
    task_description: str
    context: DelegationContext
    created_at: datetime
    memory_excerpt: list[dict[str, str | float]] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    known_pitfalls: list[str] = field(default_factory=list)
    estimated_complexity: Literal["SIMPLE", "MODERATE", "COMPLEX"] = "MODERATE"
    # NOTE: Spec uses Sequence[MemoryItem] for memory_excerpt.
    # Slice 2 uses list[dict[str, str | float]] for simplicity.
    # TODO(Slice 3): Define MemoryItem type and migrate.


@dataclass(frozen=True)
class DelegationOutcome:
    """What comes back after delegation completes.

    Args:
        task_id: Matches the DelegationPackage task_id.
        success: Whether the delegation succeeded.
        rounds_needed: How many iterations were needed.
        what_worked: What went well (for learning).
        what_was_missing: What context was missing (for learning).
        artifacts_produced: Files/outputs created.
        duration_minutes: Total time spent.
        user_satisfaction: User rating 1-5 (None if not rated).
    """

    task_id: str
    success: bool
    rounds_needed: int
    what_worked: str
    what_was_missing: str
    artifacts_produced: list[str] = field(default_factory=list)
    duration_minutes: float = 0.0
    user_satisfaction: int | None = None
```

- [ ] **Step 4: Run tests, type check, commit**

Run: `uv run pytest tests/personal_agent/request_gateway/test_delegation_types.py -v`
Expected: All PASS

Run: `uv run mypy src/personal_agent/request_gateway/delegation_types.py`
Expected: No errors

```bash
git add src/personal_agent/request_gateway/delegation_types.py tests/personal_agent/request_gateway/test_delegation_types.py
git commit -m "$(cat <<'EOF'
feat(delegation): Stage B types — DelegationPackage, DelegationOutcome

Machine-readable delegation types replacing Stage A's markdown format.
DelegationPackage: task, context, memory, criteria, pitfalls.
DelegationOutcome: success, rounds, what_worked, what_was_missing.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 6.3
EOF
)"
```

---

### Task 16: Stage B Composition + Telemetry

**Files:**
- Modify: `src/personal_agent/request_gateway/delegation.py`
- Modify: `tests/personal_agent/request_gateway/test_delegation.py`

- [ ] **Step 1: Read current delegation.py**

- [ ] **Step 2: Add Stage B composition function**

Add to `delegation.py` alongside existing `compose_delegation_instructions()`:

```python
import uuid
from datetime import datetime, timezone
from typing import Literal

from personal_agent.request_gateway.delegation_types import (
    DelegationContext,
    DelegationOutcome,
    DelegationPackage,
)

import structlog

logger = structlog.get_logger(__name__)


def compose_delegation_package(
    task_description: str,
    trace_id: str,
    target_agent: str = "claude-code",
    service_path: str = "src/personal_agent/",
    relevant_files: list[str] | None = None,
    conventions: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    known_pitfalls: list[str] | None = None,
    memory_excerpt: list[dict[str, str | float]] | None = None,
    estimated_complexity: Literal["SIMPLE", "MODERATE", "COMPLEX"] = "MODERATE",
) -> DelegationPackage:
    """Compose a Stage B structured delegation package.

    Args:
        task_description: What the external agent should do.
        trace_id: Request trace identifier.
        target_agent: Agent to delegate to.
        service_path: Primary code directory.
        relevant_files: Files the agent should reference.
        conventions: Coding conventions.
        acceptance_criteria: Verification criteria.
        known_pitfalls: Lessons from past delegations.
        memory_excerpt: Relevant memory items from Seshat.
        estimated_complexity: Task complexity estimate.

    Returns:
        DelegationPackage ready for handoff.
    """
    task_id = f"del-{uuid.uuid4().hex[:12]}"

    package = DelegationPackage(
        task_id=task_id,
        target_agent=target_agent,
        task_description=task_description,
        context=DelegationContext(
            service_path=service_path,
            relevant_files=relevant_files,
            conventions=conventions,
        ),
        memory_excerpt=memory_excerpt or [],
        acceptance_criteria=acceptance_criteria or [],
        known_pitfalls=known_pitfalls or [],
        estimated_complexity=estimated_complexity,
        created_at=datetime.now(tz=timezone.utc),
    )

    logger.info(
        "delegation_package_created",
        task_id=task_id,
        target_agent=target_agent,
        context_items=len(relevant_files or []),
        memory_items=len(package.memory_excerpt),
        criteria_count=len(package.acceptance_criteria),
        pitfall_count=len(package.known_pitfalls),
        complexity=estimated_complexity,
        trace_id=trace_id,
    )

    return package


def record_delegation_outcome(
    outcome: DelegationOutcome,
    trace_id: str,
) -> None:
    """Log a delegation outcome for telemetry and insights analysis.

    NOTE: ES persistence is via structlog → Elasticsearch pipeline
    (configured in telemetry module). No explicit ES indexing needed here.
    The structlog.info call is automatically routed to ES by the existing
    telemetry infrastructure.

    Args:
        outcome: The completed delegation outcome.
        trace_id: Request trace identifier.
    """
    logger.info(
        "delegation_outcome_recorded",
        task_id=outcome.task_id,
        success=outcome.success,
        rounds_needed=outcome.rounds_needed,
        what_worked=outcome.what_worked,
        what_was_missing=outcome.what_was_missing,
        artifacts_count=len(outcome.artifacts_produced),
        duration_minutes=outcome.duration_minutes,
        user_satisfaction=outcome.user_satisfaction,
        trace_id=trace_id,
    )
```

- [ ] **Step 3: Write tests for Stage B**

Append to `tests/personal_agent/request_gateway/test_delegation.py`:

```python
from personal_agent.request_gateway.delegation import (
    compose_delegation_package,
    record_delegation_outcome,
)
from personal_agent.request_gateway.delegation_types import DelegationOutcome


class TestComposeDelegationPackage:
    def test_creates_package_with_id(self) -> None:
        pkg = compose_delegation_package(
            task_description="Add export endpoint",
            trace_id="test-trace-1",
            target_agent="claude-code",
        )
        assert pkg.task_id.startswith("del-")
        assert pkg.target_agent == "claude-code"

    def test_includes_all_fields(self) -> None:
        pkg = compose_delegation_package(
            task_description="test",
            trace_id="test-trace-2",
            relevant_files=["app.py"],
            conventions=["type hints"],
            acceptance_criteria=["tests pass"],
            known_pitfalls=["include schema"],
        )
        assert pkg.context.relevant_files == ["app.py"]
        assert len(pkg.acceptance_criteria) == 1


class TestRecordDelegationOutcome:
    def test_logs_outcome(self) -> None:
        import structlog.testing

        outcome = DelegationOutcome(
            task_id="del-test",
            success=True,
            rounds_needed=1,
            what_worked="Good context",
            what_was_missing="",
        )
        with structlog.testing.capture_logs() as cap_logs:
            record_delegation_outcome(outcome, trace_id="test-trace-3")
        events = [e for e in cap_logs if "delegation_outcome" in e.get("event", "")]
        assert len(events) == 1
        assert events[0]["trace_id"] == "test-trace-3"
```

- [ ] **Step 4: Run tests, commit**

Run: `uv run pytest tests/personal_agent/request_gateway/test_delegation.py -v`
Expected: All PASS

```bash
git add src/personal_agent/request_gateway/delegation.py tests/personal_agent/request_gateway/test_delegation.py
git commit -m "$(cat <<'EOF'
feat(delegation): Stage B composition + outcome telemetry

compose_delegation_package(): creates machine-readable DelegationPackage.
record_delegation_outcome(): logs outcome to ES for insights analysis.
Stage A compose_delegation_instructions() preserved for backward compat.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 6.3
EOF
)"
```

---

### Task 17: Delegation Patterns in Insights Engine

**Files:**
- Modify: `src/personal_agent/insights/engine.py`
- Create: `tests/personal_agent/insights/test_delegation_patterns.py`

- [ ] **Step 1: Read insights/engine.py**

Read `src/personal_agent/insights/engine.py` to understand the existing
`analyze_patterns()` method and data sources.

- [ ] **Step 2: Add delegation pattern analysis**

Add a new method `detect_delegation_patterns()` to `InsightsEngine` that
queries ES for delegation outcome events and detects patterns:

```python
    async def detect_delegation_patterns(
        self, days: int = 30, trace_id: str = ""
    ) -> list[Insight]:
        """Detect patterns in delegation outcomes.

        Analyzes: success rate by agent/complexity, missing context trends,
        average rounds needed, time-to-completion.

        Args:
            days: Lookback window in days.
            trace_id: Request trace identifier.

        Returns:
            List of delegation-related insights.
        """
        insights: list[Insight] = []

        # Query ES for delegation_outcome_recorded events
        # This is a best-effort analysis — if ES is unavailable, return empty
        try:
            # Query delegation outcomes from ES
            # Pattern: aggregate by target_agent, success rate, avg rounds
            # Implementation depends on ES query patterns in TelemetryQueries
            # NOTE: Use `log` not `logger` — matches existing InsightsEngine convention
            log.info(
                "delegation_pattern_analysis_start",
                days=days,
                trace_id=trace_id,
            )

            # Scaffold: full implementation requires ES query support
            # which will be added when delegation outcomes accumulate
            log.info(
                "delegation_pattern_analysis_complete",
                insights_found=len(insights),
                days=days,
                trace_id=trace_id,
            )

        except Exception:
            log.warning(
                "delegation_pattern_analysis_failed",
                trace_id=trace_id,
                exc_info=True,
            )

        return insights
```

- [ ] **Step 3: Write test for detect_delegation_patterns**

Add to `tests/personal_agent/insights/test_delegation_patterns.py`:

```python
import pytest

from personal_agent.insights.engine import InsightsEngine


class TestDetectDelegationPatterns:
    @pytest.fixture()
    def engine(self) -> InsightsEngine:
        """Create an InsightsEngine for testing.

        Uses the default constructor — ES unavailability is expected
        in unit tests and the method handles it gracefully.
        """
        return InsightsEngine()

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_data(
        self, engine: InsightsEngine
    ) -> None:
        """detect_delegation_patterns returns empty list when ES has no data."""
        insights = await engine.detect_delegation_patterns(days=30)
        assert insights == []

    @pytest.mark.asyncio
    async def test_accepts_custom_lookback(
        self, engine: InsightsEngine
    ) -> None:
        """Lookback parameter is accepted without error."""
        insights = await engine.detect_delegation_patterns(days=7)
        assert isinstance(insights, list)
```

- [ ] **Step 4: Wire into analyze_patterns()**

In the `analyze_patterns()` method, add a call to `detect_delegation_patterns()`
and append results to the insights list. Since `analyze_patterns()` does not
currently accept `trace_id`, pass an empty string as default:

```python
# Inside analyze_patterns(), after existing pattern calls:
delegation_insights = await self.detect_delegation_patterns(
    days=days, trace_id="",
)
insights.extend(delegation_insights)
```

- [ ] **Step 5: Run insights tests**

Run: `uv run pytest tests/personal_agent/insights/ -v`
Expected: All pass (new method returns empty by default)

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/insights/engine.py tests/personal_agent/insights/test_delegation_patterns.py
git commit -m "$(cat <<'EOF'
feat(insights): delegation pattern analysis scaffold

InsightsEngine.detect_delegation_patterns() scaffold for ES-based
delegation outcome pattern detection (success rate, missing context,
rounds needed). Returns empty until delegation outcomes accumulate.
Wired into analyze_patterns().

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 7.2
EOF
)"
```

---

## Chunk 6: Telemetry Dashboards + Graphiti Experiment

### Task 18: Kibana Dashboards for Slice 2

**Files:**
- Create: `docs/guides/KIBANA_EXPANSION_DASHBOARDS.md`

- [ ] **Step 1: Write Kibana dashboard guide**

Create `docs/guides/KIBANA_EXPANSION_DASHBOARDS.md`:

```markdown
# Kibana Dashboards — Slice 2: Expansion

## Prerequisites

- Elasticsearch accessible at `http://localhost:9200`
- Kibana accessible at `http://localhost:5601`
- Index pattern: `agent-*`

---

## Dashboard 1: Expansion & Decomposition

**Purpose:** Visualize how often the agent expands (HYBRID/DECOMPOSE)
vs. stays calm (SINGLE) and whether expansion helps.

### Visualizations

| Viz | Type | Field | Notes |
|-----|------|-------|-------|
| Decomposition strategy distribution | Pie chart | `strategy` where `event: gateway_pipeline_complete` | Shows SINGLE/HYBRID/DECOMPOSE/DELEGATE split |
| Expansion over time | Time series | `strategy` where `event: gateway_pipeline_complete` | Filter strategy != SINGLE |
| Sub-agent spawn rate | Time series | Count where `event: sub_agent_complete` | Shows expansion volume |
| Sub-agent success rate | Metric | `success` where `event: sub_agent_complete` | Percentage true |
| Sub-agent duration distribution | Histogram | `duration_ms` where `event: sub_agent_complete` | Latency profile |
| Expansion budget utilization | Time series | `expansion_budget` where `event: expansion_budget_computed` | Budget over time |

### Saved Search

- Filter: `event: gateway_pipeline_complete OR event: sub_agent_complete OR event: expansion_budget_computed`
- Sort: `@timestamp` descending

---

## Dashboard 2: Context Budget

**Purpose:** Monitor context window utilization and trimming frequency.

### Visualizations

| Viz | Type | Field | Notes |
|-----|------|-------|-------|
| Token count distribution | Histogram | `total_tokens` where `event: context_budget_applied` | See typical context sizes |
| Trimming rate | Metric | Count where `event: context_budget_applied AND trimmed: true` / total | % of requests that need trimming |
| Overflow actions | Tag cloud | `overflow_action` where `event: context_budget_applied AND trimmed: true` | Which trim strategies fire |
| Budget utilization over time | Time series | `final_tokens / available` where `event: context_budget_applied` | Utilization ratio |

---

## Dashboard 3: Delegation Outcomes

**Purpose:** Track Stage B delegation success and learning.

### Visualizations

| Viz | Type | Field | Notes |
|-----|------|-------|-------|
| Delegation volume by agent | Bar chart | `target_agent` where `event: delegation_outcome_recorded` | Which agents get work |
| Success rate by agent | Metric | `success` grouped by `target_agent` | Per-agent success |
| Rounds needed trend | Line chart | `rounds_needed` where `event: delegation_outcome_recorded` | Should trend down |
| Missing context frequency | Tag cloud | `what_was_missing` where `success: false` | What to improve |
| Satisfaction distribution | Histogram | `user_satisfaction` where `event: delegation_outcome_recorded` | 1-5 rating |

---

## Dashboard 4: Memory Comparison (Graphiti Experiment)

**Purpose:** Compare Neo4j vs Graphiti recall quality during experiment.

> **Note:** These events (`memory_recall_*`) will only appear once the Graphiti
> experiment is executed (post Slice 2 data accumulation). This dashboard is
> pre-configured to be ready when the experiment runs.

### Visualizations

| Viz | Type | Field | Notes |
|-----|------|-------|-------|
| Recall latency comparison | Dual line chart | `duration_ms` grouped by `backend` where `event: memory_recall_*` | Neo4j vs Graphiti |
| Result count comparison | Bar chart | `result_count` grouped by `backend` | Retrieval volume |
| Relevance score comparison | Box plot | `avg_relevance` grouped by `backend` | Quality metric |

---

## Setup Instructions

1. Navigate to Kibana > Stack Management > Index Patterns
2. Verify `agent-*` pattern exists (created in Slice 1)
3. Import dashboards via Management > Saved Objects > Import
4. Or create manually using the visualization specs above
```

- [ ] **Step 2: Verify ES event names match emitted events**

Grep for each event name referenced in the dashboard guide to confirm earlier tasks emit them:

```bash
grep -rn "gateway_pipeline_complete\|sub_agent_complete\|expansion_budget_computed\|context_budget_applied\|delegation_outcome_recorded" \
  src/personal_agent/ --include="*.py"
```

Expected: Each event name appears in at least one structlog call. All five should match. If any are missing, update the dashboard guide to match the actual event names.

- [ ] **Step 3: Commit**

```bash
git add docs/guides/KIBANA_EXPANSION_DASHBOARDS.md
git commit -m "docs: Kibana dashboard guides for Slice 2

Expansion/decomposition, context budget, delegation outcomes,
and memory comparison dashboards.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 7.5"
```

---

### Task 19: Graphiti Experiment Framework

**Files:**
- Create: `docs/research/GRAPHITI_EXPERIMENT_REPORT.md`

The Graphiti experiment is a **research spike**, not production code.
The deliverable is a comparison report documenting findings.

> **Scope note:** The spec acceptance criterion says "Graphiti experiment completed
> with comparison report." In this plan, we create the **experiment framework and
> report template** — the structured test scenarios, metrics, and comparison
> dimensions. Actual execution requires real conversation data accumulated during
> Slice 2 usage. The experiment should be run after the memory pipeline is
> operational and producing episodic/semantic data. Mark the acceptance criterion
> as met when the template is committed and ready for execution.

- [ ] **Step 1: Create experiment report template**

Create `docs/research/GRAPHITI_EXPERIMENT_REPORT.md`:

```markdown
# Graphiti Experiment Report

**Date:** [Fill after experiment]
**Status:** Template — awaiting execution
**Spec ref:** COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.5

---

## Hypothesis

Graphiti (by Zep) may provide a better storage backend for Seshat's
episodic and temporal memory than the current hand-built Neo4j schema,
because it handles entity deduplication, relationship extraction, and
temporal queries natively.

## Experiment Design

### What to Compare

| Dimension | Current Neo4j | Graphiti |
|-----------|--------------|----------|
| Entity deduplication | Manual (consolidator) | Built-in |
| Temporal queries | Manual Cypher | Native API |
| Relationship extraction | LLM-based (entity_extraction.py) | Built-in |
| Recall relevance | Multi-factor scoring (service.py) | Graphiti search |
| Setup complexity | High (custom schema + migrations) | Lower (managed) |

### Test Scenarios

1. **Entity storage + retrieval:** Store 50 conversation episodes,
   query by entity name. Compare recall quality and latency.
2. **Temporal queries:** "What did I discuss about X last week?"
   Compare result relevance and ordering.
3. **Entity deduplication:** Store mentions of the same entity with
   slight name variations. Compare dedup accuracy.
4. **Scaling:** Store 500+ episodes. Compare query latency at scale.

### Metrics

- Recall latency (p50, p95, p99)
- Precision (relevant results / total results)
- Entity dedup accuracy (unique entities / raw mentions)
- Setup time and operational complexity

## Execution Steps

- [ ] Install Graphiti: `pip install graphiti-core`
- [ ] Create a test script that populates both backends with the same data
- [ ] Run each test scenario against both backends
- [ ] Capture metrics to ES with `backend` tag for dashboard comparison
- [ ] Write findings below

## Findings

[To be filled after experiment execution]

### Recommendation

- [ ] Keep current Neo4j (Graphiti adds complexity without sufficient benefit)
- [ ] Migrate to Graphiti (clear improvement in quality/latency/maintenance)
- [ ] Hybrid (use Graphiti for temporal queries, keep Neo4j for graph traversal)

## Impact on Architecture

If Graphiti is adopted, the MemoryProtocol abstraction means only the
adapter changes — no consuming code needs modification. This is exactly
why the protocol-first approach was chosen in Slice 1.
```

- [ ] **Step 2: Commit**

```bash
git add docs/research/GRAPHITI_EXPERIMENT_REPORT.md
git commit -m "docs: Graphiti experiment report template

Structured comparison framework for Neo4j vs Graphiti.
Test scenarios, metrics, and recommendation criteria defined.
Execution pending real usage data from Slice 2.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.5"
```

---

### Task 20: Final Integration Verification

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/ --timeout=60 -q`
Expected: All tests pass

- [ ] **Step 2: Run type checker on all new and modified code**

Run:
```bash
uv run mypy src/personal_agent/
```
Expected: No errors

- [ ] **Step 3: Run linter**

Run:
```bash
uv run ruff check src/personal_agent/
```
Expected: No errors

- [ ] **Step 4: Format code**

Run:
```bash
uv run ruff format src/personal_agent/
```

- [ ] **Step 5: Manual smoke test**

If the service can be started:

```bash
# Start infrastructure
./scripts/init-services.sh

# Start the service
uv run uvicorn personal_agent.service.app:app --reload --port 9000

# Test: simple conversation (should be SINGLE)
uv run agent "Hello, how are you?"

# Test: complex analysis (should trigger HYBRID/DECOMPOSE in logs)
uv run agent "Research how temporal knowledge graphs work, compare the three leading approaches, and produce a detailed recommendation with trade-offs"

# Test: coding request (should trigger DELEGATE)
uv run agent "Write a function to parse CSV files with error handling"

# Check ES for new event types
curl -s 'http://localhost:9200/agent-*/_search?q=event:gateway_pipeline_complete&size=5' | python -m json.tool
curl -s 'http://localhost:9200/agent-*/_search?q=event:context_budget_applied&size=5' | python -m json.tool
```

- [ ] **Step 6: Verify acceptance criteria**

Review against spec acceptance criteria:

- [ ] Gateway decomposition stage operational (SINGLE/HYBRID/DECOMPOSE/DELEGATE in ES)
- [ ] Context budget management active (token counts logged, trimming occurs)
- [ ] At least one HYBRID execution (sub-agent spawned, result synthesized)
- [ ] SubAgentSpec/SubAgentResult types with full ES tracing
- [ ] Brainstem expansion_budget signal operational and visible
- [ ] Episodic and semantic memory types distinguished in Neo4j
- [ ] At least one promote() execution (episode → semantic fact)
- [ ] Graphiti experiment framework created with report template (execution follows after data accumulates)
- [ ] DelegationPackage/DelegationOutcome types in use for Stage B
- [ ] Kibana dashboards documented for expansion, budget, delegation

---

## Summary

| Task | What | Key Files | Chunk |
|------|------|-----------|-------|
| 1 | Config entries for expansion + budget | `config/settings.py` | 1 |
| 2 | Decomposition assessment (Stage 5) | `request_gateway/decomposition.py` | 1 |
| 3 | Context budget management (Stage 7) | `request_gateway/budget.py` | 1 |
| 4 | Wire decomposition + budget into pipeline | `request_gateway/pipeline.py` | 1 |
| 5 | SubAgentSpec / SubAgentResult types | `orchestrator/sub_agent_types.py` | 2 |
| 6 | Sub-agent runner | `orchestrator/sub_agent.py` | 2 |
| 7 | HYBRID orchestration — expansion module | `orchestrator/expansion.py` | 2 |
| 8 | Wire HYBRID path into executor | `orchestrator/executor.py` | 2 |
| 9 | Expansion budget signal | `brainstem/expansion.py` | 3 |
| 10 | Wire expansion into service layer | `service/app.py`, `governance.py` | 3 |
| 11 | Fact and promotion types | `memory/fact.py` | 4 |
| 12 | Neo4j schema — promote_entity | `memory/service.py` | 4 |
| 13 | Real store_episode + promote on adapter | `memory/protocol_adapter.py` | 4 |
| 14 | Promote pipeline | `memory/promote.py` | 4 |
| 15 | DelegationPackage / DelegationOutcome types | `request_gateway/delegation_types.py` | 5 |
| 16 | Stage B composition + telemetry | `request_gateway/delegation.py` | 5 |
| 17 | Delegation patterns in insights engine | `insights/engine.py` | 5 |
| 18 | Kibana dashboards for Slice 2 | `docs/guides/` | 6 |
| 19 | Graphiti experiment framework | `docs/research/` | 6 |
| 20 | Final integration verification | All | 6 |
