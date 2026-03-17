"""Tests for request gateway types."""

from dataclasses import FrozenInstanceError

import pytest

from personal_agent.governance.models import Mode
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
    """Tests for TaskType enum."""

    def test_all_task_types_defined(self) -> None:
        """Verify all expected task type values exist."""
        assert TaskType.CONVERSATIONAL.value == "conversational"
        assert TaskType.MEMORY_RECALL.value == "memory_recall"
        assert TaskType.ANALYSIS.value == "analysis"
        assert TaskType.PLANNING.value == "planning"
        assert TaskType.DELEGATION.value == "delegation"
        assert TaskType.SELF_IMPROVE.value == "self_improve"
        assert TaskType.TOOL_USE.value == "tool_use"


class TestComplexity:
    """Tests for Complexity enum."""

    def test_all_complexity_levels_defined(self) -> None:
        """Verify all expected complexity levels exist."""
        assert Complexity.SIMPLE.value == "simple"
        assert Complexity.MODERATE.value == "moderate"
        assert Complexity.COMPLEX.value == "complex"


class TestIntentResult:
    """Tests for IntentResult frozen dataclass."""

    def test_construction(self) -> None:
        """Verify IntentResult can be constructed with expected fields."""
        result = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=["no_special_patterns"],
        )
        assert result.task_type == TaskType.CONVERSATIONAL
        assert result.confidence == 0.9

    def test_frozen(self) -> None:
        """Verify IntentResult is immutable (frozen dataclass)."""
        result = IntentResult(
            task_type=TaskType.ANALYSIS,
            complexity=Complexity.MODERATE,
            confidence=0.8,
            signals=["reasoning_patterns"],
        )
        with pytest.raises(FrozenInstanceError):
            result.task_type = TaskType.PLANNING  # type: ignore[misc]


class TestDecompositionResult:
    """Tests for DecompositionResult frozen dataclass."""

    def test_default_single(self) -> None:
        """Verify DecompositionResult defaults work correctly."""
        result = DecompositionResult(
            strategy=DecompositionStrategy.SINGLE,
            reason="simple conversational request",
        )
        assert result.strategy == DecompositionStrategy.SINGLE
        assert result.constraints is None


class TestGovernanceContext:
    """Tests for GovernanceContext frozen dataclass."""

    def test_default_permissive(self) -> None:
        """Verify GovernanceContext defaults are permissive."""
        ctx = GovernanceContext(
            mode=Mode.NORMAL,
            expansion_permitted=True,
        )
        assert ctx.expansion_permitted is True
        assert ctx.cost_budget_remaining is None


class TestAssembledContext:
    """Tests for AssembledContext frozen dataclass."""

    def test_frozen(self) -> None:
        """Verify AssembledContext field references are immutable."""
        context = AssembledContext(
            messages=[{"role": "user", "content": "hello"}],
            memory_context=None,
            tool_definitions=None,
        )
        # Field references are frozen, but mutable container contents are not
        # (Python frozen dataclass semantics -- by design)
        with pytest.raises(FrozenInstanceError):
            context.messages = []  # type: ignore[misc]


class TestGatewayOutput:
    """Tests for GatewayOutput frozen dataclass."""

    def test_construction_with_all_fields(self) -> None:
        """Verify GatewayOutput can be constructed with all sub-objects."""
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

    def test_frozen(self) -> None:
        """Verify GatewayOutput is immutable (frozen dataclass)."""
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
        with pytest.raises(FrozenInstanceError):
            output.session_id = "changed"  # type: ignore[misc]
