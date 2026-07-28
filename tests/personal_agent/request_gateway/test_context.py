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
    async def test_token_estimation_does_not_crash_on_list_content(self) -> None:
        """List-shaped content in history does not raise TypeError during token estimation (FRE-753)."""
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        )
        history = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "what's in this image?"}],
            },
        ]
        result = await assemble_context(
            user_message="follow up",
            session_messages=history,
            intent=intent,
            memory_adapter=None,
            trace_id="test",
        )
        assert result.token_count > 0

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
    async def test_digestless_episode_marks_rather_than_silently_clips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0125 D5 — the "worst instance": an episode with no digest, on the
        entity-name-match fallback path (proactive recall disabled or empty),
        must carry an explicit marker on its user-message fallback, not a
        silent 200-char clip.
        """
        monkeypatch.setattr(ctx_module.settings, "proactive_memory_enabled", False)
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        )
        long_user_message = "considering the tradeoffs between options " * 20  # > 800 chars
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.recall = AsyncMock(
            return_value=MagicMock(
                entities=[],
                episodes=[
                    {
                        "turn_id": "turn-9",
                        "user_message": long_user_message,
                        "summary": None,
                        "key_entities": [],
                    }
                ],
            )
        )

        result = await assemble_context(
            user_message="What about Athens?",
            session_messages=[{"role": "user", "content": "we discussed options"}],
            intent=intent,
            memory_adapter=mock_adapter,
            trace_id="t-episode",
            session_id="sess-1",
        )
        assert result.memory_context is not None
        mock_adapter.recall.assert_awaited_once()
        summary = result.memory_context[0]["summary"]
        assert len(summary) < len(long_user_message)
        assert "...[truncated" in summary

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


class TestReflectionRecallRemoved:
    """ADR-0125 D2/AC-2 (FRE-1003): the reflection-recall path is removed, not
    merely defaulted off. A dimension-1 producer's output must never be able
    to reach user-facing context under any configuration.

    A red-phase characterization test (seeding a sentinel doc through a faked
    ``query_relevant_reflections`` with ``reflection_recall_enabled=True``)
    was run against the pre-fix code to confirm the marker really did leak
    through ``assemble_context()`` — see the FRE-1003 PR/ticket for that
    evidence. That test is not preserved here: once the call site and module
    are gone there is nothing left to characterize, and a live-path assertion
    would be vacuous. What remains provable, and is asserted below, is the
    literal AC-2 wording — no call site or import remains, the module itself
    is gone, and the settings are gone rather than defaulted.
    """

    def test_reflection_recall_module_is_removed(self) -> None:
        import importlib

        with pytest.raises(ModuleNotFoundError) as exc_info:
            importlib.import_module("personal_agent.captains_log.recall")
        assert exc_info.value.name == "personal_agent.captains_log.recall"

    def test_context_assembly_has_no_reflection_recall_reference(self) -> None:
        import inspect

        source = inspect.getsource(ctx_module)
        for needle in (
            "captains_log.recall",
            "query_relevant_reflections",
            "format_reflections_section",
            "reflection_recall",
        ):
            assert needle not in source, f"stale reflection-recall reference found: {needle!r}"

    def test_reflection_recall_settings_are_gone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for field in (
            "reflection_recall_enabled",
            "reflection_recall_recency_days",
            "reflection_recall_max_results",
            "reflection_recall_min_seen_count",
        ):
            assert not hasattr(ctx_module.settings, field), f"stale settings field: {field!r}"

        # A stale prod .env line setting these legacy keys must not break startup —
        # extra="ignore" silently drops them rather than re-creating the attribute.
        monkeypatch.setenv("AGENT_REFLECTION_RECALL_ENABLED", "true")
        monkeypatch.setenv("AGENT_REFLECTION_RECALL_RECENCY_DAYS", "365")
        monkeypatch.setenv("AGENT_REFLECTION_RECALL_MAX_RESULTS", "10")
        monkeypatch.setenv("AGENT_REFLECTION_RECALL_MIN_SEEN_COUNT", "1")

        from personal_agent.config.settings import AppConfig

        config = AppConfig()
        assert not hasattr(config, "reflection_recall_enabled")
        assert not hasattr(config, "reflection_recall_recency_days")
        assert not hasattr(config, "reflection_recall_max_results")
        assert not hasattr(config, "reflection_recall_min_seen_count")


class TestSessionTopicHint:
    """Tests for _session_topic_hint() (ADR-0101 §2 content-widening, FRE-726)."""

    def test_list_content_extracts_real_text_not_repr(self) -> None:
        """List-shaped content yields its text block, not a Python-repr string."""
        session_messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "What's in this diagram?"}],
            },
        ]
        hint = ctx_module._session_topic_hint(session_messages)
        assert hint == "What's in this diagram?"
        assert "[{" not in (hint or "")

    def test_mixed_str_and_list_content(self) -> None:
        """A session mixing plain-string and list-shaped user turns joins both cleanly."""
        session_messages = [
            {"role": "user", "content": "first question"},
            {
                "role": "user",
                "content": [{"type": "text", "text": "second question"}],
            },
        ]
        hint = ctx_module._session_topic_hint(session_messages)
        assert hint == "first question second question"

    def test_image_only_content_returns_none_when_no_text_anywhere(self) -> None:
        """An image-only turn with no text anywhere yields None, not an empty string."""
        session_messages = [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
        ]
        assert ctx_module._session_topic_hint(session_messages) is None

    def test_image_only_recent_turn_does_not_evict_earlier_text(self) -> None:
        """An image-only turn contributes no empty slot, so parts[-3:] keeps prior text."""
        session_messages = [
            {"role": "user", "content": "turn one"},
            {"role": "user", "content": "turn two"},
            {"role": "user", "content": "turn three"},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
        ]
        hint = ctx_module._session_topic_hint(session_messages)
        assert hint == "turn one turn two turn three"
