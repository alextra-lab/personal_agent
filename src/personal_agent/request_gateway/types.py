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

    Attributes:
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

    Attributes:
        mode: Current brainstem operational mode.
        expansion_permitted: Whether expansion is safe given resource state.
        expansion_budget: Remaining expansion slots (0 = exhausted, force SINGLE).
        cost_budget_remaining: Remaining API cost budget (None = unlimited).
        allowed_tool_categories: Deprecated — always None since ADR-0063 §D1
            (FRE-260). The TaskType→tool-filter wire was severed; mode is the
            only tool-availability signal. Field retained for one release;
            removal scheduled for PIVOT-6 (FRE-265).
    """

    mode: Mode
    expansion_permitted: bool
    expansion_budget: int = 3
    cost_budget_remaining: float | None = None
    allowed_tool_categories: list[str] | None = None  # deprecated — see docstring


@dataclass(frozen=True)
class DecompositionResult:
    """Output of Stage 5: Decomposition Assessment.

    Attributes:
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

    Attributes:
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


@dataclass(frozen=True)
class GatewayOutput:
    """Complete output of the request gateway pipeline.

    This is the single object passed to the executor's step_init().

    Attributes:
        intent: Classified intent from Stage 4.
        governance: Governance context from Stage 3.
        decomposition: Decomposition strategy from Stage 5.
        context: Assembled and budgeted context from Stages 6+7.
        session_id: Active session identifier.
        trace_id: Request trace identifier.
        degraded_stages: Stages that degraded gracefully (for telemetry).
        recall_context: Recall controller result (Stage 4b), if triggered.
    """

    intent: IntentResult
    governance: GovernanceContext
    decomposition: DecompositionResult
    context: AssembledContext
    session_id: str
    trace_id: str
    degraded_stages: list[str] = field(default_factory=list)
    recall_context: RecallResult | None = None
