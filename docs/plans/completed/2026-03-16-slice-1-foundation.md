# Slice 1: Foundation — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace model-role routing with task-type intent classification, simplify the executor to always use the 35B primary model, define the Seshat MemoryProtocol, and add Stage A delegation instruction composition.

**Architecture:** A new `request_gateway` module implements a deterministic pipeline (security → session → governance → intent → decomposition → context → budget) that runs before the LLM. The executor receives pre-assembled context via a `GatewayOutput` dataclass. The `MemoryProtocol` abstracts memory operations behind a `typing.Protocol` that the existing `MemoryService` implements as a wrapper.

**Tech Stack:** Python 3.12+, FastAPI, structlog, Pydantic, Neo4j (async driver), Elasticsearch, pytest, mypy

**Spec:** `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` — Section 8.1

---

## Acceptance Criteria (from spec)

- [ ] All requests route through the gateway pipeline (no direct executor entry)
- [ ] Intent classification events appear in ES with task type and confidence
- [ ] Role-switching removed: `resolve_role()` gone, all requests use 35B
- [ ] MemoryProtocol defined with at least `recall()` and `store_episode()`
- [ ] Existing MemoryService passes as MemoryProtocol implementation (tests)
- [ ] Agent can produce a markdown delegation instruction package for a sample task
- [ ] Gateway degradation: agent responds when Neo4j is down (without memory)
- [ ] Kibana dashboard shows intent classification distribution

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `src/personal_agent/request_gateway/__init__.py` | Package init, exports `run_gateway_pipeline` |
| `src/personal_agent/request_gateway/types.py` | `TaskType`, `Complexity`, `IntentResult`, `GovernanceContext`, `DecompositionResult`, `AssembledContext`, `GatewayOutput` |
| `src/personal_agent/request_gateway/intent.py` | Deterministic intent classification (evolved from `routing.py` heuristics) |
| `src/personal_agent/request_gateway/governance.py` | Wraps existing `mode_manager` for governance stage |
| `src/personal_agent/request_gateway/context.py` | Context assembly from session + memory + tools |
| `src/personal_agent/request_gateway/pipeline.py` | Orchestrates all stages into a single `run_gateway_pipeline()` call |
| `src/personal_agent/memory/protocol.py` | `MemoryProtocol` abstract interface |
| `src/personal_agent/memory/protocol_adapter.py` | Adapter wrapping `MemoryService` to satisfy `MemoryProtocol` |
| `tests/personal_agent/request_gateway/__init__.py` | Test package |
| `tests/personal_agent/request_gateway/test_types.py` | Type construction and validation tests |
| `tests/personal_agent/request_gateway/test_intent.py` | Intent classification tests |
| `tests/personal_agent/request_gateway/test_governance.py` | Governance stage tests |
| `tests/personal_agent/request_gateway/test_context.py` | Context assembly tests |
| `tests/personal_agent/request_gateway/test_pipeline.py` | Pipeline integration tests |
| `tests/personal_agent/memory/test_protocol.py` | Protocol definition and adapter tests |

### Modified Files

| File | Changes |
|------|---------|
| `src/personal_agent/orchestrator/executor.py` | `step_init()` simplified to receive `GatewayOutput`. Remove inline routing/memory logic. `step_llm_call()` always uses primary model role |
| `src/personal_agent/orchestrator/types.py` | Add `TaskType` re-export. Remove `HeuristicRoutingPlan` (moved to gateway) |
| `src/personal_agent/orchestrator/routing.py` | Deprecated — functions moved to `request_gateway/intent.py`. File kept temporarily with forwarding imports for backward compat |
| `src/personal_agent/service/app.py` | Wire gateway pipeline before orchestrator call |
| `src/personal_agent/memory/service.py` | No changes to logic — adapter wraps it |
| `config/models.yaml` | Comment out router role |

---

## Chunk 1: Gateway Types and Intent Classification

### Task 1: Define Gateway Types

**Files:**
- Create: `src/personal_agent/request_gateway/__init__.py`
- Create: `src/personal_agent/request_gateway/types.py`
- Create: `tests/personal_agent/request_gateway/__init__.py`
- Create: `tests/personal_agent/request_gateway/test_types.py`

- [ ] **Step 1: Create test file with type construction tests**

```python
# tests/personal_agent/request_gateway/test_types.py
"""Tests for request gateway types."""

from personal_agent.request_gateway.types import (
    AssembledContext,
    Complexity,
    DecompositionResult,
    DecompositionStrategy,
    GatewayOutput,
    GovernanceContext,
    IntentResult,
    TaskType,
)


class TestTaskType:
    def test_all_task_types_defined(self) -> None:
        assert TaskType.CONVERSATIONAL.value == "conversational"
        assert TaskType.MEMORY_RECALL.value == "memory_recall"
        assert TaskType.ANALYSIS.value == "analysis"
        assert TaskType.PLANNING.value == "planning"
        assert TaskType.DELEGATION.value == "delegation"
        assert TaskType.SELF_IMPROVE.value == "self_improve"
        assert TaskType.TOOL_USE.value == "tool_use"

    def test_complexity_levels(self) -> None:
        assert Complexity.SIMPLE.value == "simple"
        assert Complexity.MODERATE.value == "moderate"
        assert Complexity.COMPLEX.value == "complex"


class TestIntentResult:
    def test_construction(self) -> None:
        result = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=["no_special_patterns"],
        )
        assert result.task_type == TaskType.CONVERSATIONAL
        assert result.confidence == 0.9

    def test_frozen(self) -> None:
        result = IntentResult(
            task_type=TaskType.ANALYSIS,
            complexity=Complexity.MODERATE,
            confidence=0.8,
            signals=["reasoning_patterns"],
        )
        try:
            result.task_type = TaskType.PLANNING  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass


class TestDecompositionResult:
    def test_default_single(self) -> None:
        result = DecompositionResult(
            strategy=DecompositionStrategy.SINGLE,
            reason="simple conversational request",
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.constraints is None


class TestGovernanceContext:
    def test_default_permissive(self) -> None:
        from personal_agent.governance.models import Mode

        ctx = GovernanceContext(
            mode=Mode.NORMAL,
            expansion_permitted=True,
        )
        assert ctx.expansion_permitted is True
        assert ctx.cost_budget_remaining is None


class TestGatewayOutput:
    def test_construction_with_all_fields(self) -> None:
        from personal_agent.governance.models import Mode

        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        )
        governance = GovernanceContext(
            mode=Mode.NORMAL,
            expansion_permitted=True,
        )
        decomposition = DecompositionResult(
            strategy=DecompositionStrategy.SINGLE,
            reason="simple",
        )
        context = AssembledContext(
            messages=[{"role": "user", "content": "hello"}],
            memory_context=None,
            tool_definitions=None,
            token_count=10,
            trimmed=False,
        )
        output = GatewayOutput(
            intent=intent,
            governance=governance,
            decomposition=decomposition,
            context=context,
            session_id="test-session",
            trace_id="test-trace",
        )
        assert output.intent.task_type == TaskType.CONVERSATIONAL
        assert output.session_id == "test-session"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/request_gateway/test_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'personal_agent.request_gateway'`

- [ ] **Step 3: Create the package and types module**

```python
# src/personal_agent/request_gateway/__init__.py
"""Request Gateway — deterministic pre-LLM pipeline.

Implements the seven-stage gateway from the Cognitive Architecture
Redesign v2 spec (docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md).
"""
```

```python
# src/personal_agent/request_gateway/types.py
"""Types for the request gateway pipeline.

All types are frozen dataclasses for immutability (Principle: Cherny).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from personal_agent.governance.models import Mode


class TaskType(Enum):
    """Intent classification task types.

    Replaces model-role routing (STANDARD/REASONING/CODING) with
    semantic task types that drive context assembly and decomposition.
    """

    CONVERSATIONAL = "conversational"
    MEMORY_RECALL = "memory_recall"
    ANALYSIS = "analysis"
    PLANNING = "planning"
    DELEGATION = "delegation"
    SELF_IMPROVE = "self_improve"
    TOOL_USE = "tool_use"


class Complexity(Enum):
    """Estimated task complexity.

    Drives decomposition decisions in the gateway pipeline.
    """

    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


class DecompositionStrategy(Enum):
    """How the primary agent should handle this request.

    SINGLE: Handle in one context window (calm state).
    HYBRID: Primary agent + sub-agents (moderate expansion).
    DECOMPOSE: Full task decomposition into sub-agents.
    DELEGATE: Route to external agent (Claude Code, Codex, etc.).
    """

    SINGLE = "single"
    HYBRID = "hybrid"
    DECOMPOSE = "decompose"
    DELEGATE = "delegate"


@dataclass(frozen=True)
class IntentResult:
    """Output of Stage 4: Intent Classification.

    Args:
        task_type: Classified task type.
        complexity: Estimated complexity level.
        confidence: Classification confidence (0.0-1.0).
        signals: List of matched pattern names for observability.
    """

    task_type: TaskType
    complexity: Complexity
    confidence: float
    signals: list[str]


@dataclass(frozen=True)
class GovernanceContext:
    """Output of Stage 3: Governance.

    Args:
        mode: Current brainstem operational mode.
        expansion_permitted: Whether expansion is safe given resource state.
        cost_budget_remaining: Remaining API cost budget (None = unlimited).
        allowed_tool_categories: Tool categories permitted in this mode.
    """

    mode: Mode
    expansion_permitted: bool
    cost_budget_remaining: float | None = None
    allowed_tool_categories: list[str] | None = None


@dataclass(frozen=True)
class DecompositionResult:
    """Output of Stage 5: Decomposition Assessment.

    Args:
        strategy: How the request should be handled.
        reason: Human-readable explanation for observability.
        constraints: Additional constraints (e.g., max sub-agents).
    """

    strategy: DecompositionStrategy
    reason: str
    constraints: dict[str, Any] | None = None


@dataclass(frozen=True)
class AssembledContext:
    """Output of Stage 6+7: Context Assembly + Budget.

    Args:
        messages: Final message list for the LLM (system + history + user).
        memory_context: Seshat memory enrichment (if any).
        tool_definitions: Filtered tool definitions for the LLM.
        skills: Skill definitions for the LLM (Slice 2).
        delegation_context: Delegation context for external agents (Slice 2).
        token_count: Estimated total token count.
        trimmed: Whether context was trimmed to fit budget.
        overflow_action: What was done if over budget (None = fit fine).
    """

    messages: list[dict[str, Any]]
    memory_context: list[dict[str, Any]] | None
    tool_definitions: list[dict[str, Any]] | None
    skills: list[dict[str, Any]] | None = None  # Slice 2: skill loading
    delegation_context: dict[str, Any] | None = None  # Slice 2: delegation
    token_count: int = 0
    trimmed: bool = False
    overflow_action: str | None = None


@dataclass(frozen=True)
class GatewayOutput:
    """Complete output of the request gateway pipeline.

    This is the single object passed to the executor's step_init().

    Args:
        intent: Classified intent from Stage 4.
        governance: Governance context from Stage 3.
        decomposition: Decomposition strategy from Stage 5.
        context: Assembled and budgeted context from Stages 6+7.
        session_id: Active session identifier.
        trace_id: Request trace identifier.
        degraded_stages: Stages that degraded gracefully (for telemetry).
    """

    intent: IntentResult
    governance: GovernanceContext
    decomposition: DecompositionResult
    context: AssembledContext
    session_id: str
    trace_id: str
    degraded_stages: list[str] = field(default_factory=list)
```

- [ ] **Step 4: Create test package init**

```python
# tests/personal_agent/request_gateway/__init__.py
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/personal_agent/request_gateway/test_types.py -v`
Expected: All tests PASS

- [ ] **Step 6: Run type checker**

Run: `uv run mypy src/personal_agent/request_gateway/types.py`
Expected: Success, no errors

- [ ] **Step 7: Commit**

```bash
git add src/personal_agent/request_gateway/ tests/personal_agent/request_gateway/
git commit -m "feat(gateway): define gateway types — TaskType, IntentResult, GatewayOutput

New types for the request gateway pipeline replacing model-role routing
with task-type intent classification.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 3"
```

---

### Task 2: Intent Classification

**Files:**
- Create: `src/personal_agent/request_gateway/intent.py`
- Create: `tests/personal_agent/request_gateway/test_intent.py`

- [ ] **Step 1: Write intent classification tests**

```python
# tests/personal_agent/request_gateway/test_intent.py
"""Tests for intent classification — Stage 4 of the gateway pipeline."""

import pytest

from personal_agent.request_gateway.intent import classify_intent
from personal_agent.request_gateway.types import Complexity, TaskType


class TestMemoryRecall:
    """Memory recall patterns from routing.py _MEMORY_RECALL_PATTERNS."""

    @pytest.mark.parametrize(
        "message",
        [
            "What have I asked about before?",
            "Do you remember our conversation about Python?",
            "What topics have we discussed?",
            "Last time we talked about Neo4j, what did I say?",
            "What did I decide about the architecture?",
        ],
    )
    def test_memory_recall_detected(self, message: str) -> None:
        result = classify_intent(message)
        assert result.task_type == TaskType.MEMORY_RECALL
        assert result.confidence >= 0.8

    def test_memory_recall_includes_signal(self) -> None:
        result = classify_intent("What have I asked about?")
        assert "memory_recall_pattern" in result.signals


class TestCoding:
    """Coding patterns — now classified as DELEGATION not CODING role."""

    @pytest.mark.parametrize(
        "message",
        [
            "Write a function to sort a list",
            "Debug this Python code: def foo(): pass",
            "Refactor the routing module",
            "```python\nprint('hello')\n```",
            "Fix the CI failure in tests/",
        ],
    )
    def test_coding_classified_as_delegation(self, message: str) -> None:
        result = classify_intent(message)
        assert result.task_type == TaskType.DELEGATION
        assert "coding_pattern" in result.signals


class TestAnalysis:
    """Reasoning/analysis patterns from _REASONING_PATTERNS."""

    @pytest.mark.parametrize(
        "message",
        [
            "Analyze the trade-offs between Neo4j and Graphiti",
            "Think step-by-step about the memory architecture",
            "Research how temporal knowledge graphs work",
            "Compare the three approaches and recommend one",
        ],
    )
    def test_analysis_detected(self, message: str) -> None:
        result = classify_intent(message)
        assert result.task_type == TaskType.ANALYSIS

    def test_complex_analysis(self) -> None:
        msg = (
            "Research how Graphiti handles temporal memory, "
            "compare it with our Neo4j approach, and draft "
            "a detailed recommendation with benchmarks"
        )
        result = classify_intent(msg)
        assert result.task_type == TaskType.ANALYSIS
        assert result.complexity == Complexity.COMPLEX


class TestToolUse:
    """Explicit tool intent patterns from _TOOL_INTENT_PATTERNS."""

    @pytest.mark.parametrize(
        "message",
        [
            "Search for files matching *.py",
            "List the tools available",
            "Read the config file",
            "Open the Neo4j browser",
        ],
    )
    def test_tool_use_detected(self, message: str) -> None:
        result = classify_intent(message)
        assert result.task_type == TaskType.TOOL_USE


class TestSelfImprove:
    """Self-improvement patterns — agent discussing its own architecture."""

    @pytest.mark.parametrize(
        "message",
        [
            "How could we improve the memory system?",
            "What changes would you propose to your own architecture?",
            "Review your recent Captain's Log proposals",
            "What improvements have you identified?",
        ],
    )
    def test_self_improve_detected(self, message: str) -> None:
        result = classify_intent(message)
        assert result.task_type == TaskType.SELF_IMPROVE


class TestPlanning:
    """Planning patterns."""

    @pytest.mark.parametrize(
        "message",
        [
            "Plan the next sprint",
            "Break this feature into tasks",
            "Create a roadmap for the memory system",
            "Outline the implementation steps",
        ],
    )
    def test_planning_detected(self, message: str) -> None:
        result = classify_intent(message)
        assert result.task_type == TaskType.PLANNING


class TestConversational:
    """Default — simple conversation."""

    @pytest.mark.parametrize(
        "message",
        [
            "Hello",
            "How are you?",
            "What's the weather like?",
            "Tell me a joke",
            "Thanks for your help",
        ],
    )
    def test_conversational_default(self, message: str) -> None:
        result = classify_intent(message)
        assert result.task_type == TaskType.CONVERSATIONAL
        assert result.complexity == Complexity.SIMPLE


class TestComplexityEstimation:
    """Complexity heuristics based on message properties."""

    def test_short_message_is_simple(self) -> None:
        result = classify_intent("Hello")
        assert result.complexity == Complexity.SIMPLE

    def test_long_message_bumps_complexity(self) -> None:
        msg = "Please " + "analyze this carefully. " * 30
        result = classify_intent(msg)
        assert result.complexity in (Complexity.MODERATE, Complexity.COMPLEX)

    def test_multiple_questions_bumps_complexity(self) -> None:
        msg = "What is X? How does Y work? Why did Z happen?"
        result = classify_intent(msg)
        assert result.complexity in (Complexity.MODERATE, Complexity.COMPLEX)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/request_gateway/test_intent.py -v`
Expected: FAIL — `cannot import name 'classify_intent'`

- [ ] **Step 3: Implement intent classification**

```python
# src/personal_agent/request_gateway/intent.py
"""Stage 4: Intent Classification.

Deterministic (regex + heuristics) classification of user messages
into task types. Evolved from orchestrator/routing.py heuristic_routing().

The LLM is NOT used for classification — that is the entire point.
Classification drives context assembly, not model selection.
"""

from __future__ import annotations

import re

from personal_agent.request_gateway.types import (
    Complexity,
    IntentResult,
    TaskType,
)

# ---------------------------------------------------------------------------
# Pattern banks — evolved from orchestrator/routing.py
# ---------------------------------------------------------------------------

_MEMORY_RECALL_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:what\s+(?:have\s+I|did\s+I|topics?|things?)\s+)"
    r"|(?:do\s+you\s+remember)"
    r"|(?:last\s+time\s+we)"
    r"|(?:have\s+(?:we|I)\s+(?:discussed|talked|spoken))"
    r"|(?:what\s+(?:do\s+you\s+know|did\s+(?:I|we))\s+)"
    r"|(?:recall\s+(?:our|my|the)\s+)"
    r"|(?:what\s+(?:have\s+)?(?:I|we)\s+(?:decided|concluded|said))",
)

_CODING_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:```)"
    r"|(?:(?:def|class|import|from)\s+\w+)"
    r"|(?:(?:debug|refactor|implement|fix|write)\s+(?:the|this|a|my)?\s*"
    r"(?:code|function|class|module|test|endpoint|bug|CI|pipeline))"
    r"|(?:traceback|stack\s*trace|error\s*log)"
    r"|(?:(?:unit|integration)\s*test)"
    r"|(?:pull\s*request|PR\s+review|code\s+review)",
)

_CODING_KEYWORDS: tuple[str, ...] = (
    "write a function",
    "code review",
    "unit test",
    "integration test",
    "fix the bug",
    "implement the",
    "add an endpoint",
    "refactor",
)

_ANALYSIS_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:(?:analyze|analyse|research|investigate|evaluate|compare)\s+)"
    r"|(?:think\s+step[\s-]*by[\s-]*step)"
    r"|(?:(?:deep|thorough|rigorous|careful)\s+(?:analysis|thinking|review))"
    r"|(?:trade[\s-]*offs?\s+(?:between|of))"
    r"|(?:(?:pros?\s+and\s+cons?|advantages?\s+and\s+disadvantages?))"
    r"|(?:recommend(?:ation)?s?\s+(?:for|on|about))",
)

_TOOL_INTENT_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:(?:search|find|look\s*up|grep|glob)\s+(?:for\s+)?)"
    r"|(?:(?:list|show|display)\s+(?:the\s+)?(?:tools?|files?|endpoints?))"
    r"|(?:(?:read|open|view)\s+(?:the\s+)?(?:file|config|log|url))"
    r"|(?:run\s+(?:the\s+)?(?:command|script|test))",
)

_SELF_IMPROVE_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:(?:improve|optimize|enhance|change)\s+(?:your|the|my)\s+"
    r"(?:own|architecture|system|memory|routing|performance))"
    r"|(?:captain'?s?\s+log)"
    r"|(?:(?:your|the\s+agent'?s?)\s+(?:proposals?|improvements?|suggestions?))"
    r"|(?:what\s+(?:changes|improvements)\s+(?:would\s+you|have\s+you|do\s+you))"
    r"|(?:modify\s+(?:yourself|your\s+own))",
)

_PLANNING_PATTERNS: re.Pattern[str] = re.compile(
    r"(?i)"
    r"(?:(?:plan|outline|roadmap|break\s*down|decompose)\s+)"
    r"|(?:(?:create|write|draft)\s+(?:a\s+)?(?:plan|roadmap|timeline|schedule))"
    r"|(?:(?:next|upcoming)\s+(?:sprint|iteration|phase|steps?))"
    r"|(?:(?:implementation|project)\s+(?:plan|steps|phases?))"
    r"|(?:break\s+(?:this|it)\s+into\s+(?:tasks|steps|pieces))",
)

# ---------------------------------------------------------------------------
# Complexity estimation
# ---------------------------------------------------------------------------

_QUESTION_MARK_RE: re.Pattern[str] = re.compile(r"\?")
_SENTENCE_RE: re.Pattern[str] = re.compile(r"[.!?]+")


def _estimate_complexity(message: str, task_type: TaskType) -> Complexity:
    """Estimate task complexity from message properties.

    Args:
        message: The user's message text.
        task_type: Already-classified task type (some types bias complexity).

    Returns:
        Estimated complexity level.
    """
    word_count = len(message.split())
    question_count = len(_QUESTION_MARK_RE.findall(message))
    sentence_count = max(1, len(_SENTENCE_RE.split(message)))

    # Short simple messages
    if word_count < 15 and question_count <= 1:
        return Complexity.SIMPLE

    # Multiple questions suggest moderate+
    if question_count >= 3:
        return Complexity.COMPLEX

    # Long messages with analysis intent
    if word_count > 100 and task_type in (TaskType.ANALYSIS, TaskType.PLANNING):
        return Complexity.COMPLEX

    # Medium messages or multi-sentence
    if word_count > 40 or sentence_count > 3 or question_count >= 2:
        return Complexity.MODERATE

    return Complexity.SIMPLE


# ---------------------------------------------------------------------------
# Main classification
# ---------------------------------------------------------------------------


def classify_intent(user_message: str) -> IntentResult:
    """Classify user message into a task type with complexity estimate.

    Deterministic classification using regex patterns and heuristics.
    No LLM call. This is Stage 4 of the gateway pipeline.

    Priority order (first match wins):
    1. Memory recall (highest priority — specific intent)
    2. Self-improvement (agent self-referential)
    3. Coding (maps to DELEGATION — coding delegates externally)
    4. Planning
    5. Analysis/reasoning
    6. Tool use
    7. Conversational (default)

    Args:
        user_message: The raw user message text.

    Returns:
        IntentResult with task type, complexity, confidence, and signals.
    """
    signals: list[str] = []
    msg_lower = user_message.lower()

    # 1. Memory recall
    if _MEMORY_RECALL_PATTERNS.search(user_message):
        signals.append("memory_recall_pattern")
        task_type = TaskType.MEMORY_RECALL
        confidence = 0.9
        complexity = _estimate_complexity(user_message, task_type)
        return IntentResult(
            task_type=task_type,
            complexity=complexity,
            confidence=confidence,
            signals=signals,
        )

    # 2. Self-improvement
    if _SELF_IMPROVE_PATTERNS.search(user_message):
        signals.append("self_improve_pattern")
        task_type = TaskType.SELF_IMPROVE
        confidence = 0.85
        complexity = _estimate_complexity(user_message, task_type)
        return IntentResult(
            task_type=task_type,
            complexity=complexity,
            confidence=confidence,
            signals=signals,
        )

    # 3. Coding → DELEGATION
    if _CODING_PATTERNS.search(user_message) or any(
        kw in msg_lower for kw in _CODING_KEYWORDS
    ):
        signals.append("coding_pattern")
        task_type = TaskType.DELEGATION
        confidence = 0.85
        complexity = _estimate_complexity(user_message, task_type)
        return IntentResult(
            task_type=task_type,
            complexity=complexity,
            confidence=confidence,
            signals=signals,
        )

    # 4. Planning
    if _PLANNING_PATTERNS.search(user_message):
        signals.append("planning_pattern")
        task_type = TaskType.PLANNING
        confidence = 0.8
        complexity = _estimate_complexity(user_message, task_type)
        return IntentResult(
            task_type=task_type,
            complexity=complexity,
            confidence=confidence,
            signals=signals,
        )

    # 5. Analysis / reasoning
    if _ANALYSIS_PATTERNS.search(user_message):
        signals.append("analysis_pattern")
        task_type = TaskType.ANALYSIS
        confidence = 0.8
        complexity = _estimate_complexity(user_message, task_type)
        return IntentResult(
            task_type=task_type,
            complexity=complexity,
            confidence=confidence,
            signals=signals,
        )

    # 6. Tool use
    if _TOOL_INTENT_PATTERNS.search(user_message):
        signals.append("tool_intent_pattern")
        task_type = TaskType.TOOL_USE
        confidence = 0.8
        complexity = Complexity.SIMPLE
        return IntentResult(
            task_type=task_type,
            complexity=complexity,
            confidence=confidence,
            signals=signals,
        )

    # 7. Default: conversational
    signals.append("no_special_patterns")
    return IntentResult(
        task_type=TaskType.CONVERSATIONAL,
        complexity=Complexity.SIMPLE,
        confidence=0.7,
        signals=signals,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/personal_agent/request_gateway/test_intent.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run type checker and linter**

Run: `uv run mypy src/personal_agent/request_gateway/intent.py && uv run ruff check src/personal_agent/request_gateway/`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/request_gateway/intent.py tests/personal_agent/request_gateway/test_intent.py
git commit -m "feat(gateway): intent classification — Stage 4

Deterministic regex+heuristic classification replacing model-role routing.
Classifies into 7 task types: CONVERSATIONAL, MEMORY_RECALL, ANALYSIS,
PLANNING, DELEGATION, SELF_IMPROVE, TOOL_USE.

Coding patterns now map to DELEGATION (coding delegates externally).
Complexity estimation based on message length, question count, sentences.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 3.4"
```

---

## Chunk 2: Memory Protocol

### Task 3: Define MemoryProtocol

**Files:**
- Create: `src/personal_agent/memory/protocol.py`
- Create: `tests/personal_agent/memory/test_protocol.py`

- [ ] **Step 1: Write protocol definition tests**

```python
# tests/personal_agent/memory/test_protocol.py
"""Tests for MemoryProtocol definition and adapter."""

from __future__ import annotations

from typing import runtime_checkable

import pytest

from personal_agent.memory.protocol import (
    Episode,
    MemoryProtocol,
    MemoryType,
    RecallScope,
)


class TestMemoryTypes:
    def test_all_types_defined(self) -> None:
        assert MemoryType.WORKING.value == "working"
        assert MemoryType.EPISODIC.value == "episodic"
        assert MemoryType.SEMANTIC.value == "semantic"
        assert MemoryType.PROCEDURAL.value == "procedural"
        assert MemoryType.PROFILE.value == "profile"
        assert MemoryType.DERIVED.value == "derived"

    def test_recall_scope(self) -> None:
        assert RecallScope.ALL.value == "all"
        assert RecallScope.EPISODIC.value == "episodic"
        assert RecallScope.SEMANTIC.value == "semantic"


class TestEpisode:
    def test_construction(self) -> None:
        from datetime import datetime, timezone

        ep = Episode(
            turn_id="turn-123",
            session_id="session-456",
            timestamp=datetime.now(tz=timezone.utc),
            user_message="Hello",
            assistant_response="Hi there",
            tools_used=[],
            entities=["greeting"],
        )
        assert ep.turn_id == "turn-123"
        assert ep.session_id == "session-456"


class TestProtocolIsRuntimeCheckable:
    def test_protocol_is_runtime_checkable(self) -> None:
        assert runtime_checkable(MemoryProtocol)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/memory/test_protocol.py -v`
Expected: FAIL — `cannot import name 'MemoryProtocol'`

- [ ] **Step 3: Implement MemoryProtocol**

```python
# src/personal_agent/memory/protocol.py
"""Seshat Memory Protocol — abstract memory interface.

Defines the contract that all memory implementations must satisfy.
The first implementation wraps the existing MemoryService.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.4
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class MemoryType(Enum):
    """Six memory types with different lifecycles."""

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    PROFILE = "profile"
    DERIVED = "derived"


class RecallScope(Enum):
    """Filter for which memory types to search."""

    ALL = "all"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    DERIVED = "derived"


@dataclass(frozen=True)
class Episode:
    """A single interaction episode.

    Wraps TurnNode + context for storage.

    Args:
        turn_id: Unique identifier (typically the trace_id).
        session_id: Session this episode belongs to.
        timestamp: When the episode occurred.
        user_message: What the user said.
        assistant_response: What the agent replied.
        tools_used: Tool names invoked during this episode.
        entities: Entity names extracted from the episode.
    """

    turn_id: str
    session_id: str
    timestamp: datetime
    user_message: str
    assistant_response: str | None
    tools_used: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MemoryRecallQuery:
    """Query parameters for memory recall.

    Evolves the existing MemoryQuery with memory type filtering.

    Args:
        entity_names: Filter by entity names.
        entity_types: Filter by entity types.
        memory_types: Which memory types to search (default: all).
        recency_days: Only return memories from the last N days.
        limit: Maximum results to return.
        query_text: Free-text query for relevance scoring.
    """

    entity_names: list[str] = field(default_factory=list)
    entity_types: list[str] = field(default_factory=list)
    memory_types: list[RecallScope] = field(
        default_factory=lambda: [RecallScope.ALL]
    )
    recency_days: int | None = 30
    limit: int = 10
    query_text: str | None = None


@dataclass(frozen=True)
class MemoryRecallResult:
    """Result of a memory recall query.

    Args:
        episodes: Matched episodic memories.
        entities: Matched entity information.
        relevance_scores: Per-result relevance scores.
    """

    episodes: list[dict[str, Any]]
    entities: list[dict[str, Any]]
    relevance_scores: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class BroadRecallResult:
    """Result of a broad recall query ("what have I asked about?").

    Args:
        entities_by_type: Entities grouped by type.
        recent_sessions: Recent session summaries.
        total_entity_count: Total entities in memory.
    """

    entities_by_type: dict[str, list[dict[str, Any]]]
    recent_sessions: list[dict[str, Any]]
    total_entity_count: int


@runtime_checkable
class MemoryProtocol(Protocol):
    """Abstract memory interface — the Seshat contract.

    All memory access goes through this protocol. Implementations
    can be swapped (Neo4j, Graphiti, AgentDB) without changing
    consuming code.

    Slice 1 implements: recall, recall_broad, store_episode.
    Remaining methods are stubs until Slice 2/3.
    """

    async def recall(
        self, query: MemoryRecallQuery, trace_id: str
    ) -> MemoryRecallResult:
        """Query memory for relevant episodes and entities."""
        ...

    async def recall_broad(
        self,
        entity_types: list[str] | None,
        recency_days: int,
        limit: int,
        trace_id: str,
    ) -> BroadRecallResult:
        """Broad recall for open-ended memory queries."""
        ...

    async def store_episode(
        self, episode: Episode, trace_id: str
    ) -> str:
        """Store a new episode in episodic memory. Returns episode ID."""
        ...

    async def is_connected(self) -> bool:
        """Check if the memory backend is reachable."""
        ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/personal_agent/memory/test_protocol.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run type checker**

Run: `uv run mypy src/personal_agent/memory/protocol.py`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/memory/protocol.py tests/personal_agent/memory/test_protocol.py
git commit -m "feat(memory): define MemoryProtocol — Seshat abstract interface

Runtime-checkable Protocol defining the Seshat contract:
recall(), recall_broad(), store_episode(), is_connected().

Includes Episode, MemoryRecallQuery, MemoryRecallResult,
BroadRecallResult, MemoryType, RecallScope types.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.4"
```

---

### Task 4: MemoryService Protocol Adapter

**Files:**
- Create: `src/personal_agent/memory/protocol_adapter.py`
- Modify: `tests/personal_agent/memory/test_protocol.py` (add adapter tests)

- [ ] **Step 1: Write adapter tests**

Append to `tests/personal_agent/memory/test_protocol.py`:

```python
# Add to tests/personal_agent/memory/test_protocol.py

from unittest.mock import AsyncMock, MagicMock, patch

from personal_agent.memory.protocol import (
    BroadRecallResult,
    MemoryRecallQuery,
    MemoryRecallResult,
)
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter


class TestMemoryServiceAdapter:
    """Verify adapter satisfies MemoryProtocol."""

    def test_adapter_satisfies_protocol(self) -> None:
        mock_service = MagicMock()
        adapter = MemoryServiceAdapter(service=mock_service)
        assert isinstance(adapter, MemoryProtocol)

    @pytest.mark.asyncio
    async def test_recall_delegates_to_query_memory(self) -> None:
        mock_service = MagicMock()
        mock_service.query_memory = AsyncMock(
            return_value=MagicMock(
                conversations=[],
                entities=[],
                relevance_scores={},
            )
        )
        adapter = MemoryServiceAdapter(service=mock_service)
        query = MemoryRecallQuery(entity_names=["Neo4j"], limit=5)

        result = await adapter.recall(query, trace_id="test-trace")

        assert isinstance(result, MemoryRecallResult)
        mock_service.query_memory.assert_called_once()

    @pytest.mark.asyncio
    async def test_recall_broad_delegates(self) -> None:
        mock_service = MagicMock()
        mock_service.query_memory_broad = AsyncMock(
            return_value={
                "entities_by_type": {},
                "sessions": [],
                "total_entities": 0,
            }
        )
        adapter = MemoryServiceAdapter(service=mock_service)

        result = await adapter.recall_broad(
            entity_types=None, recency_days=90, limit=20, trace_id="test"
        )

        assert isinstance(result, BroadRecallResult)

    @pytest.mark.asyncio
    async def test_is_connected_when_driver_exists(self) -> None:
        mock_service = MagicMock()
        mock_service.driver = MagicMock()
        adapter = MemoryServiceAdapter(service=mock_service)

        result = await adapter.is_connected()
        assert result is True

    @pytest.mark.asyncio
    async def test_is_connected_when_no_driver(self) -> None:
        mock_service = MagicMock()
        mock_service.driver = None
        adapter = MemoryServiceAdapter(service=mock_service)

        result = await adapter.is_connected()
        assert result is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/memory/test_protocol.py::TestMemoryServiceAdapter -v`
Expected: FAIL — `cannot import name 'MemoryServiceAdapter'`

- [ ] **Step 3: Implement the adapter**

```python
# src/personal_agent/memory/protocol_adapter.py
"""Adapter wrapping MemoryService to satisfy MemoryProtocol.

This is the Slice 1 implementation — wraps the existing MemoryService
without adding new capabilities. Enables protocol-based consumption
while the underlying service remains unchanged.
"""

from __future__ import annotations

import structlog

from personal_agent.memory.models import MemoryQuery
from personal_agent.memory.protocol import (
    BroadRecallResult,
    Episode,
    MemoryRecallQuery,
    MemoryRecallResult,
)
from personal_agent.memory.service import MemoryService

logger = structlog.get_logger(__name__)


class MemoryServiceAdapter:
    """Adapts MemoryService to the MemoryProtocol interface.

    Args:
        service: The existing MemoryService instance.
    """

    def __init__(self, service: MemoryService) -> None:
        self._service = service

    async def recall(
        self, query: MemoryRecallQuery, trace_id: str
    ) -> MemoryRecallResult:
        """Query memory by converting protocol types to service types.

        Args:
            query: Protocol-level recall query.
            trace_id: Request trace identifier.

        Returns:
            Recall result with episodes, entities, and relevance scores.
        """
        service_query = MemoryQuery(
            entity_names=query.entity_names,
            entity_types=query.entity_types,
            recency_days=query.recency_days,
            limit=query.limit,
        )
        result = await self._service.query_memory(
            service_query,
            feedback_key=trace_id,
            query_text=query.query_text,
        )
        return MemoryRecallResult(
            episodes=[
                {
                    "turn_id": c.turn_id,
                    "session_id": c.session_id,
                    "timestamp": c.timestamp.isoformat() if c.timestamp else None,
                    "summary": c.summary,
                    "user_message": c.user_message,
                    "assistant_response": c.assistant_response,
                    "key_entities": c.key_entities,
                }
                for c in result.conversations
            ],
            entities=[
                {
                    "entity_id": e.entity_id,
                    "name": e.name,
                    "entity_type": e.entity_type,
                    "description": e.description,
                    "mention_count": e.mention_count,
                }
                for e in result.entities
            ],
            relevance_scores=result.relevance_scores,
        )

    async def recall_broad(
        self,
        entity_types: list[str] | None,
        recency_days: int,
        limit: int,
        trace_id: str,
    ) -> BroadRecallResult:
        """Broad recall delegating to query_memory_broad().

        Args:
            entity_types: Filter by entity types (None = all).
            recency_days: Lookback window in days.
            limit: Maximum entities to return.
            trace_id: Request trace identifier.

        Returns:
            Broad recall result with entities grouped by type.
        """
        raw = await self._service.query_memory_broad(
            entity_types=entity_types or [],
            recency_days=recency_days,
            limit=limit,
        )
        return BroadRecallResult(
            entities_by_type=raw.get("entities_by_type", {}),
            recent_sessions=raw.get("sessions", []),
            total_entity_count=raw.get("total_entities", 0),
        )

    async def store_episode(self, episode: Episode, trace_id: str) -> str:
        """Store episode — Slice 1 stub.

        Full implementation in Slice 2 when episodic/semantic distinction
        is added. For now, logs the intent without persisting (consolidation
        handles persistence via the existing SecondBrainConsolidator).

        Args:
            episode: The episode to store.
            trace_id: Request trace identifier.

        Returns:
            The episode's turn_id.
        """
        logger.info(
            "memory_store_episode_stub",
            turn_id=episode.turn_id,
            session_id=episode.session_id,
            trace_id=trace_id,
        )
        return episode.turn_id

    async def is_connected(self) -> bool:
        """Check if the underlying Neo4j driver is available.

        Returns:
            True if the driver is initialized.
        """
        return self._service.driver is not None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/personal_agent/memory/test_protocol.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run type checker**

Run: `uv run mypy src/personal_agent/memory/protocol_adapter.py`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/memory/protocol_adapter.py tests/personal_agent/memory/test_protocol.py
git commit -m "feat(memory): MemoryServiceAdapter — protocol wrapper over existing service

Wraps MemoryService to satisfy MemoryProtocol without changing existing
logic. Converts between protocol types (MemoryRecallQuery) and service
types (MemoryQuery). store_episode() is a stub pending Slice 2.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.4"
```

---

## Chunk 3: Gateway Pipeline and Executor Simplification

### Task 5: Governance Stage

**Files:**
- Create: `src/personal_agent/request_gateway/governance.py`
- Create: `tests/personal_agent/request_gateway/test_governance.py`

- [ ] **Step 1: Write governance tests**

```python
# tests/personal_agent/request_gateway/test_governance.py
"""Tests for Stage 3: Governance."""

from personal_agent.governance.models import Mode
from personal_agent.request_gateway.governance import evaluate_governance
from personal_agent.request_gateway.types import GovernanceContext


class TestEvaluateGovernance:
    def test_normal_mode_permits_expansion(self) -> None:
        result = evaluate_governance(mode=Mode.NORMAL)
        assert result.mode == Mode.NORMAL
        assert result.expansion_permitted is True

    def test_alert_mode_disables_expansion(self) -> None:
        result = evaluate_governance(mode=Mode.ALERT)
        assert result.expansion_permitted is False

    def test_degraded_mode_disables_expansion(self) -> None:
        result = evaluate_governance(mode=Mode.DEGRADED)
        assert result.expansion_permitted is False

    def test_lockdown_mode_disables_expansion(self) -> None:
        result = evaluate_governance(mode=Mode.LOCKDOWN)
        assert result.expansion_permitted is False

    def test_recovery_mode_disables_expansion(self) -> None:
        result = evaluate_governance(mode=Mode.RECOVERY)
        assert result.expansion_permitted is False

    def test_default_mode_is_normal(self) -> None:
        result = evaluate_governance()
        assert result.mode == Mode.NORMAL
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/request_gateway/test_governance.py -v`
Expected: FAIL — `cannot import name 'evaluate_governance'`

- [ ] **Step 3: Implement governance stage**

```python
# src/personal_agent/request_gateway/governance.py
"""Stage 3: Governance.

Wraps the existing brainstem mode_manager to produce a GovernanceContext.
In Slice 1, this is a thin wrapper. Resource-aware gating and cost
budgeting are added in Slice 2.
"""

from __future__ import annotations

import structlog

from personal_agent.governance.models import Mode
from personal_agent.request_gateway.types import GovernanceContext

logger = structlog.get_logger(__name__)

# Modes that disable expansion (resource pressure or safety concern)
_EXPANSION_DISABLED_MODES: frozenset[Mode] = frozenset(
    {Mode.ALERT, Mode.DEGRADED, Mode.LOCKDOWN, Mode.RECOVERY}
)


def evaluate_governance(
    mode: Mode = Mode.NORMAL,
) -> GovernanceContext:
    """Evaluate governance constraints for this request.

    Args:
        mode: Current brainstem operational mode.

    Returns:
        GovernanceContext with mode and expansion permission.
    """
    expansion_permitted = mode not in _EXPANSION_DISABLED_MODES

    logger.debug(
        "governance_evaluated",
        mode=mode.value,
        expansion_permitted=expansion_permitted,
    )

    return GovernanceContext(
        mode=mode,
        expansion_permitted=expansion_permitted,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/personal_agent/request_gateway/test_governance.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/personal_agent/request_gateway/governance.py tests/personal_agent/request_gateway/test_governance.py
git commit -m "feat(gateway): governance stage — Stage 3

Thin wrapper over brainstem mode_manager producing GovernanceContext.
Expansion disabled in ALERT, DEGRADED, LOCKDOWN, RECOVERY modes.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 3.3"
```

---

### Task 6: Context Assembly Stage

**Files:**
- Create: `src/personal_agent/request_gateway/context.py`
- Create: `tests/personal_agent/request_gateway/test_context.py`

- [ ] **Step 1: Write context assembly tests**

```python
# tests/personal_agent/request_gateway/test_context.py
"""Tests for Stages 6+7: Context Assembly and Budget."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.request_gateway.context import assemble_context
from personal_agent.request_gateway.types import (
    AssembledContext,
    Complexity,
    IntentResult,
    TaskType,
)


class TestAssembleContext:
    @pytest.mark.asyncio
    async def test_basic_assembly_includes_user_message(self) -> None:
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        )
        result = await assemble_context(
            user_message="Hello",
            session_messages=[],
            intent=intent,
            memory_adapter=None,
            trace_id="test",
        )
        assert isinstance(result, AssembledContext)
        # Should have at least the user message
        assert any(m.get("role") == "user" for m in result.messages)

    @pytest.mark.asyncio
    async def test_session_history_included(self) -> None:
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        )
        history = [
            {"role": "user", "content": "previous question"},
            {"role": "assistant", "content": "previous answer"},
        ]
        result = await assemble_context(
            user_message="follow up",
            session_messages=history,
            intent=intent,
            memory_adapter=None,
            trace_id="test",
        )
        # History + new user message
        assert len(result.messages) >= 3

    @pytest.mark.asyncio
    async def test_memory_recall_queries_memory(self) -> None:
        intent = IntentResult(
            task_type=TaskType.MEMORY_RECALL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=["memory_recall_pattern"],
        )
        mock_adapter = AsyncMock()
        mock_adapter.recall_broad = AsyncMock(
            return_value=MagicMock(
                entities_by_type={"Topic": [{"name": "Python"}]},
                recent_sessions=[],
                total_entity_count=1,
            )
        )
        mock_adapter.is_connected = AsyncMock(return_value=True)

        result = await assemble_context(
            user_message="What have I asked about?",
            session_messages=[],
            intent=intent,
            memory_adapter=mock_adapter,
            trace_id="test",
        )
        assert result.memory_context is not None
        mock_adapter.recall_broad.assert_called_once()

    @pytest.mark.asyncio
    async def test_graceful_degradation_when_memory_unavailable(self) -> None:
        intent = IntentResult(
            task_type=TaskType.MEMORY_RECALL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=["memory_recall_pattern"],
        )
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=False)

        result = await assemble_context(
            user_message="What have I asked about?",
            session_messages=[],
            intent=intent,
            memory_adapter=mock_adapter,
            trace_id="test",
        )
        # Should still return a valid context without memory
        assert isinstance(result, AssembledContext)
        assert result.memory_context is None

    @pytest.mark.asyncio
    async def test_no_memory_adapter_still_works(self) -> None:
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        )
        result = await assemble_context(
            user_message="Hello",
            session_messages=[],
            intent=intent,
            memory_adapter=None,
            trace_id="test",
        )
        assert isinstance(result, AssembledContext)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/request_gateway/test_context.py -v`
Expected: FAIL — `cannot import name 'assemble_context'`

- [ ] **Step 3: Implement context assembly**

```python
# src/personal_agent/request_gateway/context.py
"""Stage 6+7: Context Assembly and Budget.

Assembles the final message list for the LLM from:
- Session history
- Seshat memory (via MemoryProtocol adapter)
- User message

In Slice 1, skill loading and budget trimming are deferred.
The budget stage is a pass-through that counts tokens.
"""

from __future__ import annotations

from typing import Any

import structlog

from personal_agent.memory.protocol import BroadRecallResult, MemoryProtocol
from personal_agent.request_gateway.types import (
    AssembledContext,
    IntentResult,
    TaskType,
)

logger = structlog.get_logger(__name__)


def _format_broad_recall_context(
    broad: BroadRecallResult,
) -> list[dict[str, Any]]:
    """Format broad recall result as memory context for the LLM.

    Args:
        broad: The broad recall result from Seshat.

    Returns:
        List of formatted memory context items.
    """
    context: list[dict[str, Any]] = []

    for entity_type, entities in broad.entities_by_type.items():
        for entity in entities:
            context.append(
                {
                    "type": "entity",
                    "entity_type": entity_type,
                    "name": entity.get("name", "unknown"),
                    "description": entity.get("description"),
                    "mention_count": entity.get("mention_count", 0),
                }
            )

    for session in broad.recent_sessions:
        context.append(
            {
                "type": "session",
                "session_id": session.get("session_id"),
                "summary": session.get("session_summary"),
                "dominant_entities": session.get("dominant_entities", []),
            }
        )

    return context


async def _query_memory_for_intent(
    intent: IntentResult,
    user_message: str,
    memory_adapter: MemoryProtocol,
    trace_id: str,
) -> list[dict[str, Any]] | None:
    """Query memory based on intent type.

    Args:
        intent: Classified intent result.
        user_message: The user's message.
        memory_adapter: Seshat protocol adapter.
        trace_id: Request trace identifier.

    Returns:
        Memory context list, or None if no relevant memory found.
    """
    try:
        if not await memory_adapter.is_connected():
            logger.warning("memory_unavailable", trace_id=trace_id)
            return None

        if intent.task_type == TaskType.MEMORY_RECALL:
            broad = await memory_adapter.recall_broad(
                entity_types=None,
                recency_days=90,
                limit=20,
                trace_id=trace_id,
            )
            return _format_broad_recall_context(broad)

        # For other intents, no memory enrichment in Slice 1.
        # Slice 2 adds entity-name matching and task-type-specific recall.
        return None

    except Exception:
        logger.exception("memory_query_failed", trace_id=trace_id)
        return None


async def assemble_context(
    user_message: str,
    session_messages: list[dict[str, Any]],
    intent: IntentResult,
    memory_adapter: MemoryProtocol | None,
    trace_id: str,
) -> AssembledContext:
    """Assemble the full context for the primary agent.

    Combines session history, memory enrichment, and user message
    into a final message list. In Slice 1, skill loading and
    budget trimming are stubs.

    Args:
        user_message: The current user message.
        session_messages: Prior conversation history (OpenAI format).
        intent: Classified intent from Stage 4.
        memory_adapter: Seshat protocol adapter (None if unavailable).
        trace_id: Request trace identifier.

    Returns:
        AssembledContext with messages and metadata.
    """
    messages: list[dict[str, Any]] = []
    memory_context: list[dict[str, Any]] | None = None

    # Include session history
    messages.extend(session_messages)

    # Query memory if adapter is available
    if memory_adapter is not None:
        memory_context = await _query_memory_for_intent(
            intent=intent,
            user_message=user_message,
            memory_adapter=memory_adapter,
            trace_id=trace_id,
        )

    # Add the current user message
    messages.append({"role": "user", "content": user_message})

    # Slice 1: simple token estimation (word count * 1.3)
    total_text = " ".join(m.get("content", "") for m in messages)
    estimated_tokens = int(len(total_text.split()) * 1.3)

    logger.debug(
        "context_assembled",
        message_count=len(messages),
        has_memory=memory_context is not None,
        estimated_tokens=estimated_tokens,
        task_type=intent.task_type.value,
        trace_id=trace_id,
    )

    return AssembledContext(
        messages=messages,
        memory_context=memory_context,
        tool_definitions=None,  # Populated by executor's existing tool logic
        token_count=estimated_tokens,
        trimmed=False,  # Slice 1: no budget trimming
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/personal_agent/request_gateway/test_context.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/personal_agent/request_gateway/context.py tests/personal_agent/request_gateway/test_context.py
git commit -m "feat(gateway): context assembly — Stages 6+7

Assembles context from session history + Seshat memory. Memory recall
queries use recall_broad() via MemoryProtocol adapter. Graceful
degradation when memory is unavailable.

Token estimation and budget trimming are stubs for Slice 1.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Sections 3.6-3.7"
```

---

### Task 7: Gateway Pipeline

**Files:**
- Create: `src/personal_agent/request_gateway/pipeline.py`
- Create: `tests/personal_agent/request_gateway/test_pipeline.py`
- Modify: `src/personal_agent/request_gateway/__init__.py`

- [ ] **Step 1: Write pipeline integration tests**

```python
# tests/personal_agent/request_gateway/test_pipeline.py
"""Tests for the full gateway pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.governance.models import Mode
from personal_agent.request_gateway.pipeline import run_gateway_pipeline
from personal_agent.request_gateway.types import (
    DecompositionStrategy,
    GatewayOutput,
    TaskType,
)


class TestRunGatewayPipeline:
    @pytest.mark.asyncio
    async def test_simple_conversational_request(self) -> None:
        result = await run_gateway_pipeline(
            user_message="Hello, how are you?",
            session_id="test-session",
            session_messages=[],
            trace_id="test-trace",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        assert isinstance(result, GatewayOutput)
        assert result.intent.task_type == TaskType.CONVERSATIONAL
        assert result.decomposition.strategy == DecompositionStrategy.SINGLE
        assert result.session_id == "test-session"
        assert result.trace_id == "test-trace"

    @pytest.mark.asyncio
    async def test_memory_recall_request(self) -> None:
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.recall_broad = AsyncMock(
            return_value=MagicMock(
                entities_by_type={"Topic": [{"name": "Python"}]},
                recent_sessions=[],
                total_entity_count=1,
            )
        )
        result = await run_gateway_pipeline(
            user_message="What have I asked about?",
            session_id="test-session",
            session_messages=[],
            trace_id="test-trace",
            mode=Mode.NORMAL,
            memory_adapter=mock_adapter,
        )
        assert result.intent.task_type == TaskType.MEMORY_RECALL
        assert result.context.memory_context is not None

    @pytest.mark.asyncio
    async def test_coding_maps_to_delegation(self) -> None:
        result = await run_gateway_pipeline(
            user_message="Write a function to sort a list",
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=None,
        )
        assert result.intent.task_type == TaskType.DELEGATION

    @pytest.mark.asyncio
    async def test_alert_mode_disables_expansion(self) -> None:
        result = await run_gateway_pipeline(
            user_message="Hello",
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.ALERT,
            memory_adapter=None,
        )
        assert result.governance.expansion_permitted is False

    @pytest.mark.asyncio
    async def test_pipeline_emits_telemetry_event(self) -> None:
        import structlog.testing

        with structlog.testing.capture_logs() as cap_logs:
            await run_gateway_pipeline(
                user_message="Hello",
                session_id="s",
                session_messages=[],
                trace_id="t",
                mode=Mode.NORMAL,
                memory_adapter=None,
            )
        pipeline_events = [
            e for e in cap_logs
            if e.get("event") == "gateway_pipeline_complete"
        ]
        assert len(pipeline_events) == 1
        assert "task_type" in pipeline_events[0]
        assert "complexity" in pipeline_events[0]
        assert "trace_id" in pipeline_events[0]

    @pytest.mark.asyncio
    async def test_degraded_stages_tracked(self) -> None:
        # Memory adapter that fails
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=False)

        result = await run_gateway_pipeline(
            user_message="What have I asked about?",
            session_id="s",
            session_messages=[],
            trace_id="t",
            mode=Mode.NORMAL,
            memory_adapter=mock_adapter,
        )
        # Context assembly should report degraded memory
        assert result.context.memory_context is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/request_gateway/test_pipeline.py -v`
Expected: FAIL — `cannot import name 'run_gateway_pipeline'`

- [ ] **Step 3: Implement the pipeline**

```python
# src/personal_agent/request_gateway/pipeline.py
"""Request Gateway Pipeline — orchestrates all stages.

Runs the deterministic pre-LLM pipeline:
  Stage 1: Security (stub in Slice 1)
  Stage 2: Session (handled externally — messages passed in)
  Stage 3: Governance
  Stage 4: Intent Classification
  Stage 5: Decomposition Assessment (always SINGLE in Slice 1)
  Stage 6+7: Context Assembly + Budget
"""

from __future__ import annotations

from typing import Any

import structlog

from personal_agent.governance.models import Mode
from personal_agent.memory.protocol import MemoryProtocol
from personal_agent.request_gateway.context import assemble_context
from personal_agent.request_gateway.governance import evaluate_governance
from personal_agent.request_gateway.intent import classify_intent
from personal_agent.request_gateway.types import (
    DecompositionResult,
    DecompositionStrategy,
    GatewayOutput,
    TaskType,
)

logger = structlog.get_logger(__name__)


async def run_gateway_pipeline(
    user_message: str,
    session_id: str,
    session_messages: list[dict[str, Any]],
    trace_id: str,
    mode: Mode = Mode.NORMAL,
    memory_adapter: MemoryProtocol | None = None,
) -> GatewayOutput:
    """Run the full request gateway pipeline.

    Executes all stages sequentially, producing a GatewayOutput
    that the executor receives as pre-assembled context.

    Args:
        user_message: The user's message text.
        session_id: Active session identifier.
        session_messages: Prior conversation messages (OpenAI format).
        trace_id: Request trace identifier.
        mode: Current brainstem operational mode.
        memory_adapter: Seshat protocol adapter (None if unavailable).

    Returns:
        GatewayOutput with intent, governance, decomposition, and context.
    """
    degraded_stages: list[str] = []

    # Stage 1: Security (stub — pass-through in Slice 1)
    # Future: rate limiting, input sanitization, PII detection

    # Stage 3: Governance
    governance = evaluate_governance(mode=mode)

    # Stage 4: Intent Classification
    intent = classify_intent(user_message)

    # Stage 5: Decomposition Assessment (always SINGLE in Slice 1)
    decomposition = DecompositionResult(
        strategy=DecompositionStrategy.SINGLE,
        reason="slice_1_always_single",
    )

    # Stage 6+7: Context Assembly + Budget
    context = await assemble_context(
        user_message=user_message,
        session_messages=session_messages,
        intent=intent,
        memory_adapter=memory_adapter,
        trace_id=trace_id,
    )

    # Track degraded memory
    if (
        memory_adapter is not None
        and context.memory_context is None
        and intent.task_type == TaskType.MEMORY_RECALL
    ):
        degraded_stages.append("context_assembly:memory_unavailable")

    output = GatewayOutput(
        intent=intent,
        governance=governance,
        decomposition=decomposition,
        context=context,
        session_id=session_id,
        trace_id=trace_id,
        degraded_stages=degraded_stages,
    )

    # Telemetry event
    logger.info(
        "gateway_pipeline_complete",
        task_type=intent.task_type.value,
        complexity=intent.complexity.value,
        confidence=intent.confidence,
        signals=intent.signals,
        mode=governance.mode.value,
        expansion_permitted=governance.expansion_permitted,
        strategy=decomposition.strategy.value,
        message_count=len(context.messages),
        token_count=context.token_count,
        has_memory=context.memory_context is not None,
        degraded_stages=degraded_stages,
        trace_id=trace_id,
    )

    return output
```

- [ ] **Step 4: Update package init**

```python
# src/personal_agent/request_gateway/__init__.py
"""Request Gateway — deterministic pre-LLM pipeline.

Implements the seven-stage gateway from the Cognitive Architecture
Redesign v2 spec (docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md).
"""

from personal_agent.request_gateway.pipeline import run_gateway_pipeline
from personal_agent.request_gateway.types import (
    AssembledContext,
    Complexity,
    DecompositionResult,
    DecompositionStrategy,
    GatewayOutput,
    GovernanceContext,
    IntentResult,
    TaskType,
)

__all__ = [
    "AssembledContext",
    "Complexity",
    "DecompositionResult",
    "DecompositionStrategy",
    "GatewayOutput",
    "GovernanceContext",
    "IntentResult",
    "TaskType",
    "run_gateway_pipeline",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/personal_agent/request_gateway/ -v`
Expected: All tests PASS

- [ ] **Step 6: Run type checker and linter on full gateway module**

Run: `uv run mypy src/personal_agent/request_gateway/ && uv run ruff check src/personal_agent/request_gateway/`
Expected: No errors

- [ ] **Step 7: Commit**

```bash
git add src/personal_agent/request_gateway/ tests/personal_agent/request_gateway/
git commit -m "feat(gateway): pipeline — orchestrates all stages

run_gateway_pipeline() executes: governance -> intent -> decomposition
-> context assembly. Returns GatewayOutput with all pre-LLM decisions.

Security stage is a stub. Decomposition always returns SINGLE in Slice 1.
Degraded stages tracked for telemetry.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 3"
```

---

## Chunk 4: Service Integration, Executor Simplification, Telemetry, Config

### Task 8: Wire Gateway into Service and Simplify Executor

This is the critical integration task. The `/chat` endpoint calls
`run_gateway_pipeline()` before the orchestrator, and the executor
receives `GatewayOutput` instead of doing inline routing.

**Files:**
- Modify: `src/personal_agent/service/app.py`
- Modify: `src/personal_agent/orchestrator/orchestrator.py`
- Modify: `src/personal_agent/orchestrator/executor.py`
- Modify: `src/personal_agent/orchestrator/types.py`

**This task requires careful incremental changes. Read each file before modifying.**

- [ ] **Step 1: Add GatewayOutput to ExecutionContext**

Read `src/personal_agent/orchestrator/types.py` first, then add a
`gateway_output` field to `ExecutionContext`:

Add after the `metrics_summary` field (around line 183):

```python
    gateway_output: GatewayOutput | None = None  # From request_gateway pipeline
```

Add the import at the top of `types.py`:

```python
from personal_agent.request_gateway.types import GatewayOutput
```

There is no circular import risk — `orchestrator/types.py` does not import
from `request_gateway`, and `request_gateway/types.py` does not import from
`orchestrator`.

- [ ] **Step 2: Run existing tests to verify nothing broke**

Run: `uv run pytest tests/ -x --timeout=30 -q`
Expected: Existing tests still pass (the new field has a default)

- [ ] **Step 3: Commit the types change**

```bash
git add src/personal_agent/orchestrator/types.py
git commit -m "feat(types): add gateway_output field to ExecutionContext

Prepares ExecutionContext to receive GatewayOutput from the
request gateway pipeline. Uses Any temporarily to avoid circular imports.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 2.4"
```

- [ ] **Step 4: Wire gateway into service /chat endpoint**

Read `src/personal_agent/service/app.py` first, then modify the `/chat`
handler to call `run_gateway_pipeline()` before the orchestrator.

Find the orchestrator call section (around line 443–470). Before the
`orchestrator.handle_user_request()` call, add the gateway pipeline:

```python
# Import at top of file
from personal_agent.governance.models import Mode
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter
from personal_agent.request_gateway import run_gateway_pipeline
```

In the `/chat` handler, after session hydration and before orchestrator call:

```python
            # --- Gateway Pipeline ---
            with timer.span("gateway_pipeline"):
                memory_adapter = (
                    MemoryServiceAdapter(service=memory_service)
                    if memory_service and memory_service.driver
                    else None
                )
                gateway_output = await run_gateway_pipeline(
                    user_message=message,
                    session_id=str(session.session_id),
                    session_messages=prior_messages,
                    trace_id=trace_id,
                    mode=Mode.NORMAL,  # From brainstem in future
                    memory_adapter=memory_adapter,
                )
```

Then pass `gateway_output` through to the orchestrator:

**Sub-step A:** Read `src/personal_agent/orchestrator/orchestrator.py`. Find the
`handle_user_request()` method. Add a `gateway_output` parameter:

```python
# In orchestrator.py, add parameter to handle_user_request():
async def handle_user_request(
    self,
    user_message: str,
    session: Any,
    # ... existing params ...
    gateway_output: GatewayOutput | None = None,  # NEW
) -> dict[str, Any]:
```

**Sub-step B:** Inside `handle_user_request()`, find where `ExecutionContext` is created
(around line 86-93). Add `gateway_output=gateway_output` to the constructor call.

**Sub-step C:** Back in `app.py`, update the `handle_user_request()` call to pass
`gateway_output=gateway_output`.

**Note:** Read each file fully before modifying. The `GatewayOutput` import in
`orchestrator.py` is: `from personal_agent.request_gateway.types import GatewayOutput`

- [ ] **Step 5: Write integration tests for the gateway-driven executor path**

Create `tests/personal_agent/orchestrator/test_gateway_integration.py`:

```python
# tests/personal_agent/orchestrator/test_gateway_integration.py
"""Tests for gateway-driven executor path."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from personal_agent.governance.models import Mode
from personal_agent.orchestrator.types import ExecutionContext
from personal_agent.request_gateway.types import (
    AssembledContext,
    Complexity,
    DecompositionResult,
    DecompositionStrategy,
    GatewayOutput,
    GovernanceContext,
    IntentResult,
    TaskType,
)


def _make_gateway_output(
    task_type: TaskType = TaskType.CONVERSATIONAL,
) -> GatewayOutput:
    """Helper to create a GatewayOutput for testing."""
    return GatewayOutput(
        intent=IntentResult(
            task_type=task_type,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        ),
        governance=GovernanceContext(
            mode=Mode.NORMAL,
            expansion_permitted=True,
        ),
        decomposition=DecompositionResult(
            strategy=DecompositionStrategy.SINGLE,
            reason="test",
        ),
        context=AssembledContext(
            messages=[{"role": "user", "content": "hello"}],
            memory_context=None,
            tool_definitions=None,
        ),
        session_id="test-session",
        trace_id="test-trace",
    )


class TestGatewayOutputOnExecutionContext:
    def test_gateway_output_stored_on_context(self) -> None:
        gw = _make_gateway_output()
        # ExecutionContext should accept gateway_output
        ctx = MagicMock(spec=ExecutionContext)
        ctx.gateway_output = gw
        assert ctx.gateway_output.intent.task_type == TaskType.CONVERSATIONAL

    def test_gateway_output_defaults_to_none(self) -> None:
        ctx = ExecutionContext.__new__(ExecutionContext)
        # The default should be None when not provided
        assert getattr(ctx, "gateway_output", None) is None
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/personal_agent/orchestrator/test_gateway_integration.py -v && uv run pytest tests/ -x --timeout=60 -q`
Expected: All tests pass

- [ ] **Step 7: Commit service integration**

```bash
git add src/personal_agent/service/app.py src/personal_agent/orchestrator/orchestrator.py tests/personal_agent/orchestrator/test_gateway_integration.py
git commit -m "feat(service): wire gateway pipeline into /chat endpoint

Gateway pipeline runs before orchestrator. GatewayOutput passed to
executor via ExecutionContext through handle_user_request(). Executor
still uses old routing in this commit — next commit switches to
gateway-driven flow.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 2"
```

- [ ] **Step 8: Simplify executor step_init to use GatewayOutput**

Read `src/personal_agent/orchestrator/executor.py`, specifically `step_init()`
(lines 806–968). The goal is to replace the inline routing and memory
logic with reading from `ctx.gateway_output`.

**Key changes to step_init():**

1. If `ctx.gateway_output` is not None, skip inline routing and memory queries
2. Use `ctx.gateway_output.intent` instead of `heuristic_routing()`
3. Use `ctx.gateway_output.context.memory_context` instead of inline memory query
4. Keep the old path as fallback for backward compatibility (when gateway_output is None)

```python
# At the top of step_init, after session message loading:

if ctx.gateway_output is not None:
    # Gateway-driven path: context already assembled
    gw = ctx.gateway_output
    if gw.context.memory_context:
        ctx.memory_context = gw.context.memory_context
    # Skip inline routing and memory logic
    return TaskState.LLM_CALL

# ... existing routing/memory code remains as fallback ...
```

- [ ] **Step 9: Simplify model selection in step_llm_call**

In `step_llm_call()`, when `ctx.gateway_output` is set, always use the
primary model (reasoning role, which maps to the 35B):

```python
# At model selection point in step_llm_call:
if ctx.gateway_output is not None:
    model_role = ModelRole.REASONING  # Always use 35B primary
else:
    model_role = resolve_role(routing_plan.get("target_model", ModelRole.STANDARD))
```

- [ ] **Step 10: Run full test suite**

Run: `uv run pytest tests/ -x --timeout=60 -q`
Expected: All tests pass

- [ ] **Step 11: Run type checker**

Run: `uv run mypy src/personal_agent/orchestrator/executor.py src/personal_agent/service/app.py`
Expected: No errors (or only pre-existing ones)

- [ ] **Step 12: Commit executor simplification**

```bash
git add src/personal_agent/orchestrator/executor.py
git commit -m "feat(executor): gateway-driven path in step_init and step_llm_call

When gateway_output is present on ExecutionContext:
- step_init() skips inline routing and memory queries
- step_llm_call() always uses REASONING role (35B primary)
- Old routing path preserved as fallback when gateway_output is None

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 2.4"
```

---

### Task 9: Intent Classification Telemetry

**Files:**
- Modify: `src/personal_agent/request_gateway/pipeline.py` (already emits log)
- Note: Elasticsearch indexing happens via existing structlog -> ES handler

- [ ] **Step 1: Verify telemetry event is emitted**

The `run_gateway_pipeline()` already emits a `gateway_pipeline_complete`
structured log event with all intent classification fields. The existing
`ElasticsearchHandler` in the service captures structlog events to ES.

Write a test that verifies the log event using structlog's test utility:

```python
# tests/personal_agent/request_gateway/test_pipeline.py
# Add this test to the existing TestRunGatewayPipeline class:

    @pytest.mark.asyncio
    async def test_pipeline_logs_intent_classification_to_es(self) -> None:
        import structlog.testing

        with structlog.testing.capture_logs() as cap_logs:
            await run_gateway_pipeline(
                user_message="Analyze the trade-offs",
                session_id="s",
                session_messages=[],
                trace_id="t",
                mode=Mode.NORMAL,
                memory_adapter=None,
            )
        events = [
            e for e in cap_logs
            if e.get("event") == "gateway_pipeline_complete"
        ]
        assert len(events) == 1
        evt = events[0]
        assert evt["task_type"] == "analysis"
        assert "confidence" in evt
        assert evt["trace_id"] == "t"
```

- [ ] **Step 2: Commit**

```bash
git add tests/personal_agent/request_gateway/test_pipeline.py
git commit -m "test(gateway): verify telemetry event emission

Gateway pipeline emits gateway_pipeline_complete structured log event
with task_type, complexity, confidence, signals, mode, strategy.
ES indexing handled by existing structlog -> ElasticsearchHandler.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 3"
```

---

### Task 10: Config Cleanup

**Files:**
- Modify: `config/models.yaml`

- [ ] **Step 1: Read current models.yaml**

Read `config/models.yaml` to see the router entry.

- [ ] **Step 2: Comment out router role**

Comment out the router model entry in `config/models.yaml` with a note:

```yaml
# Router role removed in Cognitive Architecture Redesign v2 (Slice 1).
# Intent classification is now deterministic (request_gateway/intent.py).
# See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 3.10
#
# router:
#   id: "liquid/lfm2.5-1.2b"
#   ...
```

- [ ] **Step 3: Run tests to verify nothing breaks**

Run: `uv run pytest tests/ -x --timeout=60 -q`
Expected: All tests pass (router was already optional)

- [ ] **Step 4: Commit**

```bash
git add config/models.yaml
git commit -m "config: comment out router role in models.yaml

Router SLM replaced by deterministic gateway intent classification.
Entry preserved as comment for reference.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 3.10"
```

---

### Task 11: Final Integration Verification

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/ --timeout=60 -q`
Expected: All tests pass

- [ ] **Step 2: Run type checker on all modified code**

Run: `uv run mypy src/personal_agent/request_gateway/ src/personal_agent/memory/protocol.py src/personal_agent/memory/protocol_adapter.py src/personal_agent/orchestrator/executor.py src/personal_agent/orchestrator/types.py src/personal_agent/orchestrator/orchestrator.py src/personal_agent/service/app.py`
Expected: No errors

- [ ] **Step 3: Run linter**

Run: `uv run ruff check src/personal_agent/request_gateway/ src/personal_agent/memory/protocol.py src/personal_agent/memory/protocol_adapter.py src/personal_agent/orchestrator/executor.py src/personal_agent/orchestrator/types.py src/personal_agent/service/app.py`
Expected: No errors

- [ ] **Step 4: Format code**

Run: `uv run ruff format src/personal_agent/request_gateway/ src/personal_agent/memory/protocol.py src/personal_agent/memory/protocol_adapter.py`

- [ ] **Step 5: Manual smoke test**

If the service can be started:

```bash
# Start infrastructure
./scripts/init-services.sh

# Start the service
uv run uvicorn personal_agent.service.app:app --reload --port 9000

# Send a test message
uv run agent "Hello, how are you?"
# Expected: Response from agent using 35B model

# Send a memory recall test
uv run agent "What have I asked about?"
# Expected: Response with memory context (or graceful without if Neo4j down)
```

- [ ] **Step 6: Verify self-analysis stream model indirection preserved**

The Slice 1 refactoring must not collapse the configurable model assignment for
background self-analysis streams (spec Section 4.1.1). Add two guard-rail tests:

```python
# tests/personal_agent/test_process_role_indirection.py
"""Guard-rail tests for self-analysis stream model indirection (spec 4.1.1).

These tests verify the MECHANISM — configurable process-role keys and
provider-based dispatch — not specific provider values. They must pass
regardless of whether streams point at cloud or local models.
"""

import inspect

from personal_agent.config import load_model_config


def test_process_role_keys_resolve_to_valid_models() -> None:
    """Process-role keys must resolve to entries in the models registry.

    Defense-in-depth: ModelConfig._validate_process_roles already enforces
    this at load time, but we assert it explicitly so a Slice 1 refactoring
    that removes the validator is caught immediately.
    """
    config = load_model_config()

    for role_name in ("entity_extraction_role", "captains_log_role", "insights_role"):
        role_value = getattr(config, role_name)
        assert role_value in config.models, (
            f"{role_name}={role_value!r} does not match any entry in models"
        )


def test_self_analysis_consumers_use_process_role_indirection() -> None:
    """Consumers must read model assignment from config, not hardcode a ModelRole."""
    from personal_agent.second_brain import entity_extraction
    from personal_agent.captains_log import reflection

    ee_source = inspect.getsource(entity_extraction)
    refl_source = inspect.getsource(reflection)

    # Must reference the configurable process-role key (not a hardcoded ModelRole)
    assert "entity_extraction_role" in ee_source
    assert "captains_log_role" in refl_source

    # Must branch on the provider field (the dispatch mechanism)
    assert ".provider" in ee_source
    assert ".provider" in refl_source

    # NOTE: insights/engine.py does not yet use LLM-based analysis.
    # When insights_role dispatch is added to engine.py, add assertions here.
    # See spec Section 4.1.1 invariant.
```

Run: `uv run pytest tests/personal_agent/test_process_role_indirection.py -v`
Expected: Both tests pass

- [ ] **Step 7: Verify acceptance criteria**

Review against spec acceptance criteria:

- [ ] All requests route through the gateway pipeline
- [ ] Intent classification events appear in ES
- [ ] Role-switching removed (when gateway_output present)
- [ ] MemoryProtocol defined with recall() and store_episode()
- [ ] MemoryService adapter passes protocol tests
- [ ] Gateway degradation works when Neo4j is down
- [ ] Self-analysis stream process-role indirection preserved (Section 4.1.1)
- [ ] (Delegation instruction composition — see Task 12)
- [ ] (Kibana dashboard — see Task 12)

---

### Task 12: Delegation Instruction Composition (Stage A)

**Files:**
- Create: `src/personal_agent/request_gateway/delegation.py`
- Create: `tests/personal_agent/request_gateway/test_delegation.py`

- [ ] **Step 1: Write delegation composition tests**

```python
# tests/personal_agent/request_gateway/test_delegation.py
"""Tests for Stage A delegation instruction composition."""

from personal_agent.request_gateway.delegation import (
    compose_delegation_instructions,
)


class TestComposeDelegationInstructions:
    def test_basic_delegation_package(self) -> None:
        result = compose_delegation_instructions(
            task_description="Add a GET /sessions/{id}/export endpoint",
            context_notes=["FastAPI service on port 9000"],
            conventions=["Google-style docstrings", "structlog with trace_id"],
            acceptance_criteria=["Tests pass", "Type check passes"],
        )
        assert "Add a GET /sessions/{id}/export endpoint" in result
        assert "Google-style docstrings" in result
        assert "Tests pass" in result

    def test_includes_known_pitfalls(self) -> None:
        result = compose_delegation_instructions(
            task_description="Add endpoint",
            known_pitfalls=["Include DB schema — last delegation failed without it"],
        )
        assert "Known pitfall" in result.lower() or "pitfall" in result.lower()

    def test_markdown_format(self) -> None:
        result = compose_delegation_instructions(
            task_description="Test task",
        )
        assert result.startswith("# ")  # Markdown heading
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/personal_agent/request_gateway/test_delegation.py -v`
Expected: FAIL

- [ ] **Step 3: Implement delegation instruction composition**

```python
# src/personal_agent/request_gateway/delegation.py
"""Stage A: Delegation Instruction Composition.

Produces structured markdown delegation packages that the user
can copy to external agents (Claude Code, Codex, etc.).

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 6.2
"""

from __future__ import annotations

from collections.abc import Sequence


def compose_delegation_instructions(
    task_description: str,
    context_notes: Sequence[str] | None = None,
    conventions: Sequence[str] | None = None,
    acceptance_criteria: Sequence[str] | None = None,
    known_pitfalls: Sequence[str] | None = None,
) -> str:
    """Compose a markdown delegation instruction package.

    Args:
        task_description: What the external agent should do.
        context_notes: Relevant context about the codebase/project.
        conventions: Coding conventions to follow.
        acceptance_criteria: How to verify the work is done.
        known_pitfalls: Lessons from past delegations.

    Returns:
        Formatted markdown string ready for copy-paste.
    """
    sections: list[str] = []

    sections.append(f"# Delegation Instruction Package\n\n## Task\n\n{task_description}")

    if context_notes:
        items = "\n".join(f"- {note}" for note in context_notes)
        sections.append(f"## Context\n\n{items}")

    if conventions:
        items = "\n".join(f"- {conv}" for conv in conventions)
        sections.append(f"## Conventions\n\n{items}")

    if acceptance_criteria:
        items = "\n".join(f"- [ ] {criterion}" for criterion in acceptance_criteria)
        sections.append(f"## Acceptance Criteria\n\n{items}")

    if known_pitfalls:
        items = "\n".join(f"- {pitfall}" for pitfall in known_pitfalls)
        sections.append(f"## Known Pitfalls (from memory)\n\n{items}")

    return "\n\n".join(sections) + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/personal_agent/request_gateway/test_delegation.py -v`
Expected: All tests PASS

- [ ] **Step 5: Update package exports**

Add to `src/personal_agent/request_gateway/__init__.py`:

```python
from personal_agent.request_gateway.delegation import compose_delegation_instructions

# Add to __all__:
    "compose_delegation_instructions",
```

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/request_gateway/delegation.py src/personal_agent/request_gateway/__init__.py tests/personal_agent/request_gateway/test_delegation.py
git commit -m "feat(gateway): Stage A delegation instruction composition

Produces markdown delegation packages for external agents.
Includes task, context, conventions, acceptance criteria, known pitfalls.

This is the manual copy-paste stage. Structured handoff (Stage B)
comes in Slice 2.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 6.2"
```

---

### Task 13: Kibana Dashboard (Documentation Only)

The intent classification events are already logged via structlog and
indexed to ES by the existing `ElasticsearchHandler`. A Kibana dashboard
needs to be configured manually.

- [ ] **Step 1: Document the dashboard configuration**

Create `docs/guides/KIBANA_INTENT_DASHBOARD.md` with instructions for:
- Index pattern: `agent-*` (matches existing patterns)
- Visualization 1: Pie chart of `task_type` distribution
- Visualization 2: Time series of intent classifications
- Visualization 3: Confidence distribution histogram
- Visualization 4: Signals breakdown table
- Filter: `event_type: "gateway_pipeline_complete"`

- [ ] **Step 2: Commit**

```bash
git add docs/guides/KIBANA_INTENT_DASHBOARD.md
git commit -m "docs: Kibana intent classification dashboard guide

Instructions for configuring dashboard showing task type distribution,
confidence trends, and signal breakdowns from gateway telemetry.

Ref: COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 7.5"
```

---

## Summary

| Task | What | Files | Status |
|------|------|-------|--------|
| 1 | Gateway types | `request_gateway/types.py` | |
| 2 | Intent classification | `request_gateway/intent.py` | |
| 3 | MemoryProtocol | `memory/protocol.py` | |
| 4 | Protocol adapter | `memory/protocol_adapter.py` | |
| 5 | Governance stage | `request_gateway/governance.py` | |
| 6 | Context assembly | `request_gateway/context.py` | |
| 7 | Gateway pipeline | `request_gateway/pipeline.py` | |
| 8 | Service + executor integration | `service/app.py`, `executor.py` | |
| 9 | Telemetry verification | Tests | |
| 10 | Config cleanup | `models.yaml` | |
| 11 | Integration verification | All | |
| 12 | Delegation composition | `request_gateway/delegation.py` | |
| 13 | Kibana dashboard docs | `docs/guides/` | |
