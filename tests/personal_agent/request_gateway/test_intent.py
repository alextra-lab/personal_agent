"""Tests for intent classification -- Stage 4 of the gateway pipeline."""

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
        """Verify memory recall messages are classified correctly."""
        result = classify_intent(message)
        assert result.task_type == TaskType.MEMORY_RECALL
        assert result.confidence >= 0.8

    def test_memory_recall_includes_signal(self) -> None:
        """Verify memory recall classification emits the expected signal."""
        result = classify_intent("What have I asked about?")
        assert "memory_recall_pattern" in result.signals


class TestCoding:
    """Coding patterns -- now classified as DELEGATION not CODING role."""

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
        """Verify coding messages are classified as DELEGATION."""
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
        """Verify analysis messages are classified correctly."""
        result = classify_intent(message)
        assert result.task_type == TaskType.ANALYSIS

    def test_complex_analysis(self) -> None:
        """Verify multi-step analysis requests are classified as COMPLEX."""
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
        """Verify tool intent messages are classified as TOOL_USE."""
        result = classify_intent(message)
        assert result.task_type == TaskType.TOOL_USE


class TestSelfImprove:
    """Self-improvement patterns -- agent discussing its own architecture."""

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
        """Verify self-improvement messages are classified correctly."""
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
        """Verify planning messages are classified correctly."""
        result = classify_intent(message)
        assert result.task_type == TaskType.PLANNING


class TestConversational:
    """Default -- simple conversation."""

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
        """Verify unmatched messages default to CONVERSATIONAL with SIMPLE complexity."""
        result = classify_intent(message)
        assert result.task_type == TaskType.CONVERSATIONAL
        assert result.complexity == Complexity.SIMPLE


class TestComplexityEstimation:
    """Complexity heuristics based on message properties."""

    def test_short_message_is_simple(self) -> None:
        """Verify short messages are classified as SIMPLE complexity."""
        result = classify_intent("Hello")
        assert result.complexity == Complexity.SIMPLE

    def test_long_message_bumps_complexity(self) -> None:
        """Verify long messages bump complexity to MODERATE or COMPLEX."""
        msg = "Please " + "analyze this carefully. " * 30
        result = classify_intent(msg)
        assert result.complexity in (Complexity.MODERATE, Complexity.COMPLEX)

    def test_multiple_questions_bumps_complexity(self) -> None:
        """Verify multiple questions bump complexity to MODERATE or COMPLEX."""
        msg = "What is X? How does Y work? Why did Z happen?"
        result = classify_intent(msg)
        assert result.complexity in (Complexity.MODERATE, Complexity.COMPLEX)
