"""Tests for Stage 5: Decomposition Assessment — decision matrix."""

import pytest

from personal_agent.governance.models import Mode
from personal_agent.request_gateway.decomposition import assess_decomposition
from personal_agent.request_gateway.types import (
    Complexity,
    DecompositionResult,
    DecompositionStrategy,
    GovernanceContext,
    IntentResult,
    TaskType,
)


def _intent(task_type: TaskType, complexity: Complexity = Complexity.SIMPLE) -> IntentResult:
    """Build a minimal IntentResult for testing."""
    return IntentResult(task_type=task_type, complexity=complexity, confidence=0.9, signals=[])


def _governance(
    expansion_permitted: bool = True,
    expansion_budget: int = 3,
) -> GovernanceContext:
    """Build a minimal GovernanceContext for testing."""
    return GovernanceContext(
        mode=Mode.NORMAL,
        expansion_permitted=expansion_permitted,
        expansion_budget=expansion_budget,
    )


class TestResourcePressureForcesSingle:
    """Resource pressure always forces SINGLE regardless of task/complexity."""

    def test_expansion_denied_forces_single(self) -> None:
        """expansion_permitted=False overrides any task type."""
        result = assess_decomposition(
            intent=_intent(TaskType.ANALYSIS, Complexity.COMPLEX),
            governance=_governance(expansion_permitted=False),
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "expansion_denied"

    def test_zero_budget_forces_single(self) -> None:
        """expansion_budget=0 overrides any task type."""
        result = assess_decomposition(
            intent=_intent(TaskType.PLANNING, Complexity.COMPLEX),
            governance=_governance(expansion_budget=0),
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "zero_budget"

    def test_negative_budget_forces_single(self) -> None:
        """Negative budget (exhausted) behaves same as zero."""
        result = assess_decomposition(
            intent=_intent(TaskType.ANALYSIS, Complexity.MODERATE),
            governance=_governance(expansion_budget=-1),
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "zero_budget"

    def test_expansion_denied_takes_priority_over_zero_budget(self) -> None:
        """expansion_permitted=False checked before budget."""
        result = assess_decomposition(
            intent=_intent(TaskType.DELEGATION),
            governance=_governance(expansion_permitted=False, expansion_budget=0),
        )
        assert result.reason == "expansion_denied"


class TestConversationalAlwaysSingle:
    """CONVERSATIONAL → SINGLE at every complexity level."""

    def test_conversational_simple(self) -> None:
        result = assess_decomposition(_intent(TaskType.CONVERSATIONAL, Complexity.SIMPLE), _governance())
        assert result.strategy == DecompositionStrategy.SINGLE

    def test_conversational_moderate(self) -> None:
        result = assess_decomposition(_intent(TaskType.CONVERSATIONAL, Complexity.MODERATE), _governance())
        assert result.strategy == DecompositionStrategy.SINGLE

    def test_conversational_complex(self) -> None:
        result = assess_decomposition(_intent(TaskType.CONVERSATIONAL, Complexity.COMPLEX), _governance())
        assert result.strategy == DecompositionStrategy.SINGLE


class TestMemoryRecallAlwaysSingle:
    """MEMORY_RECALL → SINGLE at every complexity level."""

    def test_memory_recall_simple(self) -> None:
        result = assess_decomposition(_intent(TaskType.MEMORY_RECALL, Complexity.SIMPLE), _governance())
        assert result.strategy == DecompositionStrategy.SINGLE

    def test_memory_recall_complex(self) -> None:
        result = assess_decomposition(_intent(TaskType.MEMORY_RECALL, Complexity.COMPLEX), _governance())
        assert result.strategy == DecompositionStrategy.SINGLE


class TestToolUseSingle:
    """TOOL_USE → SINGLE (tools execute independently)."""

    def test_tool_use_simple(self) -> None:
        result = assess_decomposition(_intent(TaskType.TOOL_USE, Complexity.SIMPLE), _governance())
        assert result.strategy == DecompositionStrategy.SINGLE

    def test_tool_use_moderate(self) -> None:
        result = assess_decomposition(_intent(TaskType.TOOL_USE, Complexity.MODERATE), _governance())
        assert result.strategy == DecompositionStrategy.SINGLE

    def test_tool_use_complex(self) -> None:
        result = assess_decomposition(_intent(TaskType.TOOL_USE, Complexity.COMPLEX), _governance())
        assert result.strategy == DecompositionStrategy.SINGLE


class TestAnalysisMatrix:
    """ANALYSIS decision matrix: SIMPLE→SINGLE, MODERATE→HYBRID, COMPLEX→DECOMPOSE."""

    def test_analysis_simple_is_single(self) -> None:
        result = assess_decomposition(_intent(TaskType.ANALYSIS, Complexity.SIMPLE), _governance())
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "analysis_simple"

    def test_analysis_moderate_is_hybrid(self) -> None:
        result = assess_decomposition(_intent(TaskType.ANALYSIS, Complexity.MODERATE), _governance())
        assert result.strategy == DecompositionStrategy.HYBRID
        assert result.reason == "analysis_moderate_hybrid"

    def test_analysis_complex_is_decompose(self) -> None:
        result = assess_decomposition(_intent(TaskType.ANALYSIS, Complexity.COMPLEX), _governance())
        assert result.strategy == DecompositionStrategy.DECOMPOSE
        assert result.reason == "analysis_complex_decompose"


class TestPlanningMatrix:
    """PLANNING: SIMPLE→SINGLE, MODERATE+→HYBRID."""

    def test_planning_simple_is_single(self) -> None:
        result = assess_decomposition(_intent(TaskType.PLANNING, Complexity.SIMPLE), _governance())
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "planning_simple"

    def test_planning_moderate_is_hybrid(self) -> None:
        result = assess_decomposition(_intent(TaskType.PLANNING, Complexity.MODERATE), _governance())
        assert result.strategy == DecompositionStrategy.HYBRID
        assert result.reason == "planning_moderate_hybrid"

    def test_planning_complex_is_hybrid(self) -> None:
        result = assess_decomposition(_intent(TaskType.PLANNING, Complexity.COMPLEX), _governance())
        assert result.strategy == DecompositionStrategy.HYBRID
        assert result.reason == "planning_moderate_hybrid"


class TestDelegationAlwaysDelegate:
    """DELEGATION → DELEGATE at every complexity level."""

    def test_delegation_simple(self) -> None:
        result = assess_decomposition(_intent(TaskType.DELEGATION, Complexity.SIMPLE), _governance())
        assert result.strategy == DecompositionStrategy.DELEGATE

    def test_delegation_complex(self) -> None:
        result = assess_decomposition(_intent(TaskType.DELEGATION, Complexity.COMPLEX), _governance())
        assert result.strategy == DecompositionStrategy.DELEGATE
        assert result.reason == "delegation_route_external"


class TestSelfImproveAlwaysSingle:
    """SELF_IMPROVE → SINGLE at every complexity level."""

    def test_self_improve_simple(self) -> None:
        result = assess_decomposition(_intent(TaskType.SELF_IMPROVE, Complexity.SIMPLE), _governance())
        assert result.strategy == DecompositionStrategy.SINGLE

    def test_self_improve_complex(self) -> None:
        result = assess_decomposition(_intent(TaskType.SELF_IMPROVE, Complexity.COMPLEX), _governance())
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "self_improve_always_single"


class TestReturnType:
    """Verify return type and immutability."""

    def test_returns_decomposition_result(self) -> None:
        result = assess_decomposition(_intent(TaskType.CONVERSATIONAL), _governance())
        assert isinstance(result, DecompositionResult)

    def test_result_is_frozen(self) -> None:
        result = assess_decomposition(_intent(TaskType.ANALYSIS, Complexity.COMPLEX), _governance())
        with pytest.raises(AttributeError):
            result.strategy = DecompositionStrategy.SINGLE  # type: ignore[misc]

    def test_result_has_reason_string(self) -> None:
        result = assess_decomposition(_intent(TaskType.ANALYSIS, Complexity.COMPLEX), _governance())
        assert isinstance(result.reason, str)
        assert len(result.reason) > 0
