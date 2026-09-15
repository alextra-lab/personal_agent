"""Tests for Stage 5: Decomposition Assessment — decision matrix."""

import pytest

from personal_agent.governance.models import Mode
from personal_agent.request_gateway.decomposition import assess_decomposition
from personal_agent.request_gateway.intent import classify_intent
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
        result = assess_decomposition(
            _intent(TaskType.CONVERSATIONAL, Complexity.SIMPLE), _governance()
        )
        assert result.strategy == DecompositionStrategy.SINGLE

    def test_conversational_moderate(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.CONVERSATIONAL, Complexity.MODERATE), _governance()
        )
        assert result.strategy == DecompositionStrategy.SINGLE

    def test_conversational_complex(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.CONVERSATIONAL, Complexity.COMPLEX), _governance()
        )
        assert result.strategy == DecompositionStrategy.SINGLE


class TestMemoryRecallAlwaysSingle:
    """MEMORY_RECALL → SINGLE at every complexity level."""

    def test_memory_recall_simple(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.MEMORY_RECALL, Complexity.SIMPLE), _governance()
        )
        assert result.strategy == DecompositionStrategy.SINGLE

    def test_memory_recall_complex(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.MEMORY_RECALL, Complexity.COMPLEX), _governance()
        )
        assert result.strategy == DecompositionStrategy.SINGLE


class TestToolUseMatrix:
    """TOOL_USE routing: unconditional SINGLE at every complexity (ADR-0086 retired FRE-884)."""

    @pytest.mark.parametrize(
        "complexity",
        [Complexity.SIMPLE, Complexity.MODERATE, Complexity.COMPLEX],
    )
    def test_tool_use_is_always_single(self, complexity: Complexity) -> None:
        """Every TOOL_USE complexity → SINGLE / tool_use_single, unconditionally."""
        result = assess_decomposition(_intent(TaskType.TOOL_USE, complexity), _governance())
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "tool_use_single"


class TestAnalysisMatrix:
    """ANALYSIS decision matrix: SIMPLE→SINGLE, MODERATE→HYBRID, COMPLEX→DECOMPOSE."""

    def test_analysis_simple_is_single(self) -> None:
        result = assess_decomposition(_intent(TaskType.ANALYSIS, Complexity.SIMPLE), _governance())
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "analysis_simple"

    def test_analysis_moderate_is_hybrid(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.ANALYSIS, Complexity.MODERATE), _governance()
        )
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
        result = assess_decomposition(
            _intent(TaskType.PLANNING, Complexity.MODERATE), _governance()
        )
        assert result.strategy == DecompositionStrategy.HYBRID
        assert result.reason == "planning_moderate_hybrid"

    def test_planning_complex_is_hybrid(self) -> None:
        result = assess_decomposition(_intent(TaskType.PLANNING, Complexity.COMPLEX), _governance())
        assert result.strategy == DecompositionStrategy.HYBRID
        assert result.reason == "planning_moderate_hybrid"


class TestDelegationGate:
    """FRE-1376: DELEGATION only routes to DELEGATE when delegation_enabled=True.

    Without a wired adapter, DELEGATE composes a DelegationPackage nothing can
    receive (three adapters exist, none configured) — so it falls back to the
    complexity-appropriate strategy, mirroring the ANALYSIS matrix shape.
    """

    def test_delegation_without_target_simple_falls_back_to_single(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.DELEGATION, Complexity.SIMPLE), _governance()
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "delegation_no_target_fallback_single"

    def test_delegation_without_target_moderate_falls_back_to_hybrid(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.DELEGATION, Complexity.MODERATE), _governance()
        )
        assert result.strategy == DecompositionStrategy.HYBRID
        assert result.reason == "delegation_no_target_fallback_hybrid"

    def test_delegation_without_target_complex_falls_back_to_decompose(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.DELEGATION, Complexity.COMPLEX), _governance()
        )
        assert result.strategy == DecompositionStrategy.DECOMPOSE
        assert result.reason == "delegation_no_target_fallback_decompose"

    def test_delegation_with_target_configured_simple_routes_delegate(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.DELEGATION, Complexity.SIMPLE),
            _governance(),
            delegation_enabled=True,
        )
        assert result.strategy == DecompositionStrategy.DELEGATE
        assert result.reason == "delegation_route_external"

    def test_delegation_with_target_configured_complex_routes_delegate(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.DELEGATION, Complexity.COMPLEX),
            _governance(),
            delegation_enabled=True,
        )
        assert result.strategy == DecompositionStrategy.DELEGATE
        assert result.reason == "delegation_route_external"

    def test_expansion_denied_overrides_delegation_enabled(self) -> None:
        """Resource pressure still forces SINGLE even when delegation is enabled."""
        result = assess_decomposition(
            _intent(TaskType.DELEGATION, Complexity.COMPLEX),
            _governance(expansion_permitted=False),
            delegation_enabled=True,
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "expansion_denied"


class TestExpansionEnabledGate:
    """FRE-1520: expansion_enabled=False forces SINGLE/expansion_disabled.

    A separate, non-mode switch for the FRE-1517 A/B study — resource-pressure
    forcings (expansion_denied, zero_budget) must still win when they apply
    (AC-3), and DELEGATE must stay unreachable even when delegation is wired.
    """

    def test_off_forces_single_for_hybrid_routing_intent(self) -> None:
        """AC-1: an intent the matrix would route HYBRID instead returns SINGLE."""
        result = assess_decomposition(
            intent=_intent(TaskType.ANALYSIS, Complexity.MODERATE),
            governance=_governance(),
            expansion_enabled=False,
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "expansion_disabled"

    def test_off_forces_single_for_decompose_routing_intent(self) -> None:
        result = assess_decomposition(
            intent=_intent(TaskType.ANALYSIS, Complexity.COMPLEX),
            governance=_governance(),
            expansion_enabled=False,
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "expansion_disabled"

    def test_off_overrides_reason_for_already_single_routing_intent(self) -> None:
        """The gate replaces the matrix reason even when the strategy was SINGLE anyway."""
        result = assess_decomposition(
            intent=_intent(TaskType.CONVERSATIONAL, Complexity.SIMPLE),
            governance=_governance(),
            expansion_enabled=False,
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "expansion_disabled"

    def test_off_suppresses_delegate_even_when_delegation_enabled(self) -> None:
        """DELEGATE must stay unreachable when expansion is disabled."""
        result = assess_decomposition(
            intent=_intent(TaskType.DELEGATION, Complexity.SIMPLE),
            governance=_governance(),
            delegation_enabled=True,
            expansion_enabled=False,
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "expansion_disabled"

    def test_on_by_default_leaves_hybrid_routing_unchanged(self) -> None:
        """AC-2: the default (expansion_enabled=True) does not change routing."""
        result = assess_decomposition(
            intent=_intent(TaskType.ANALYSIS, Complexity.MODERATE),
            governance=_governance(),
        )
        assert result.strategy == DecompositionStrategy.HYBRID
        assert result.reason == "analysis_moderate_hybrid"

    def test_expansion_denied_still_wins_over_expansion_disabled(self) -> None:
        """AC-3: governance.expansion_permitted=False keeps its own reason."""
        result = assess_decomposition(
            intent=_intent(TaskType.ANALYSIS, Complexity.MODERATE),
            governance=_governance(expansion_permitted=False),
            expansion_enabled=False,
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "expansion_denied"

    def test_zero_budget_still_wins_over_expansion_disabled(self) -> None:
        """AC-3: governance.expansion_budget<=0 keeps its own reason."""
        result = assess_decomposition(
            intent=_intent(TaskType.ANALYSIS, Complexity.MODERATE),
            governance=_governance(expansion_budget=0),
            expansion_enabled=False,
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "zero_budget"


class TestSelfImproveAlwaysSingle:
    """SELF_IMPROVE → SINGLE at every complexity level."""

    def test_self_improve_simple(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.SELF_IMPROVE, Complexity.SIMPLE), _governance()
        )
        assert result.strategy == DecompositionStrategy.SINGLE

    def test_self_improve_complex(self) -> None:
        result = assess_decomposition(
            _intent(TaskType.SELF_IMPROVE, Complexity.COMPLEX), _governance()
        )
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


# Shape of the originating trace a0a07227: explain internals + build an artifact.
_ARTIFACT_MSG = "Explain the internals of the gateway and build an interactive HTML guide."


class TestArtifactBuildRetired:
    """FRE-884 — ADR-0086's artifact-build decomposition path is retired.

    End-to-end through the REAL classifier: a genuine artifact-build message
    still routes to SINGLE, proving the retirement (no flag, no HYBRID, no
    discovery dispatch) rather than asserting against synthetic complexity.
    """

    def test_real_artifact_build_still_routes_single(self) -> None:
        result = assess_decomposition(classify_intent(_ARTIFACT_MSG), _governance())
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "tool_use_single"

    def test_real_artifact_build_governance_denied_still_single(self) -> None:
        """Resource-pressure guard still forces SINGLE (independent of the retirement)."""
        result = assess_decomposition(
            classify_intent(_ARTIFACT_MSG), _governance(expansion_permitted=False)
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.reason == "expansion_denied"
