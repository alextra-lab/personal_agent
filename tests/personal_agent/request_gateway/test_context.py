"""Tests for Stages 6+7: Context Assembly and Budget."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import personal_agent.request_gateway.context as ctx_module
from personal_agent.memory.proactive_types import (
    ProactiveMemoryCandidate,
    ProactiveMemorySuggestions,
    ProactiveScoreComponents,
)
from personal_agent.request_gateway.context import assemble_context
from personal_agent.request_gateway.types import (
    AssembledContext,
    Complexity,
    IntentResult,
    TaskType,
)


class TestAssembleContext:
    """Tests for the assemble_context() function (Stages 6+7)."""

    @pytest.mark.asyncio
    async def test_basic_assembly_includes_user_message(self) -> None:
        """Verify basic assembly includes the user message."""
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
        assert any(m.get("role") == "user" for m in result.messages)

    @pytest.mark.asyncio
    async def test_session_history_included(self) -> None:
        """Verify session history is preserved in output."""
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
        """Verify MEMORY_RECALL intent triggers recall_broad()."""
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
        """Verify graceful degradation when memory is not connected."""
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
        assert isinstance(result, AssembledContext)
        assert result.memory_context is None

    @pytest.mark.asyncio
    async def test_no_memory_adapter_still_works(self) -> None:
        """Verify context assembly works when no memory adapter is provided."""
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

    @pytest.mark.asyncio
    async def test_memory_exception_degrades_gracefully(self) -> None:
        """Verify context assembly continues when recall_broad raises."""
        intent = IntentResult(
            task_type=TaskType.MEMORY_RECALL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=["memory_recall_pattern"],
        )
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.recall_broad = AsyncMock(side_effect=RuntimeError("neo4j down"))

        result = await assemble_context(
            user_message="What have I asked about?",
            session_messages=[],
            intent=intent,
            memory_adapter=mock_adapter,
            trace_id="test-exc",
        )
        assert isinstance(result, AssembledContext)
        assert result.memory_context is None

    @pytest.mark.asyncio
    async def test_proactive_memory_enabled_uses_suggest_relevant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-recall intent with flag on calls suggest_relevant and injects payloads."""
        monkeypatch.setattr(ctx_module.settings, "proactive_memory_enabled", True)
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        )
        cand = ProactiveMemoryCandidate(
            kind="entity",
            payload={
                "type": "entity",
                "name": "Neo4j",
                "entity_type": "Technology",
                "description": None,
                "mention_count": 2,
            },
            relevance_score=0.88,
            score_components=ProactiveScoreComponents(
                embedding=0.8,
                entity_overlap=0.5,
                recency=0.6,
                topic_coherence=0.5,
            ),
        )
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.suggest_relevant = AsyncMock(
            return_value=ProactiveMemorySuggestions(candidates=[cand])
        )

        result = await assemble_context(
            user_message="Tell me about graphs",
            session_messages=[{"role": "user", "content": "we use neo4j"}],
            intent=intent,
            memory_adapter=mock_adapter,
            trace_id="t-pro",
            session_id="sess-1",
        )
        assert result.memory_context is not None
        assert result.memory_context[0]["name"] == "Neo4j"
        mock_adapter.suggest_relevant.assert_awaited_once()
        mock_adapter.recall.assert_not_called()

    @pytest.mark.asyncio
    async def test_proactive_memory_disabled_skips_suggest_relevant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag off does not call suggest_relevant when no capitalized entities."""
        monkeypatch.setattr(ctx_module.settings, "proactive_memory_enabled", False)
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        )
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)

        result = await assemble_context(
            user_message="hello there",
            session_messages=[],
            intent=intent,
            memory_adapter=mock_adapter,
            trace_id="t-off",
        )
        assert result.memory_context is None
        mock_adapter.suggest_relevant.assert_not_called()
