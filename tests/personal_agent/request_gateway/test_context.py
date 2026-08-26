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


class TestGraphAnchoredEntityHints:
    """FRE-1041: entity hints come from the graph, not a capitalisation heuristic.

    The heuristic returned every capitalised word longer than three characters, so it
    was blind to every lowercase subject (74.4 % of real turns yielded no usable name)
    and passed sentence-initial stopwords downstream as entity names (32.2 % of turns).
    Both consumers now ask the graph which of its entities the message actually names.
    """

    def test_capitalisation_heuristic_is_gone(self) -> None:
        """The replaced heuristic must not survive anywhere in the module."""
        assert not hasattr(ctx_module, "_capitalized_entity_hints")

    @pytest.mark.asyncio
    async def test_proactive_path_passes_resolved_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The resolver's output is what feeds the overlap subscore."""
        monkeypatch.setattr(ctx_module.settings, "proactive_memory_enabled", True)
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        )
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.resolve_message_entities = AsyncMock(return_value=["Melon", "Ice cream"])
        mock_adapter.suggest_relevant = AsyncMock(
            return_value=ProactiveMemorySuggestions(candidates=[])
        )
        mock_adapter.recall = AsyncMock(return_value=MagicMock(entities=[], episodes=[]))

        await assemble_context(
            user_message="I would like to make a melon/canteloupe ice cream",
            session_messages=[],
            intent=intent,
            memory_adapter=mock_adapter,
            trace_id="t-resolve",
            session_id="sess-1",
        )

        kwargs = mock_adapter.suggest_relevant.await_args.kwargs
        assert kwargs["session_entity_names"] == ["Melon", "Ice cream"]
        # FRE-1062: the same resolver output rides a second, distinct seam — the
        # admission pin. The overlap copy above gets merged with DB session entities
        # inside the adapter; this one must arrive verbatim.
        assert kwargs["mentioned_entity_names"] == ["Melon", "Ice cream"]

    @pytest.mark.asyncio
    async def test_lowercase_subject_reaches_the_entity_recall_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The decisive case: a lowercase subject now enters entity recall.

        Under the capitalisation heuristic this message produced zero hints — the only
        capitalised token is a single-character ``I`` — so the fallback path returned
        without ever querying for entities.
        """
        monkeypatch.setattr(ctx_module.settings, "proactive_memory_enabled", False)
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        )
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.resolve_message_entities = AsyncMock(return_value=["Melon"])
        mock_adapter.recall = AsyncMock(return_value=MagicMock(entities=[], episodes=[]))

        await assemble_context(
            user_message="I would like to make a melon/canteloupe ice cream",
            session_messages=[],
            intent=intent,
            memory_adapter=mock_adapter,
            trace_id="t-melon",
            session_id="sess-1",
        )

        mock_adapter.recall.assert_awaited_once()
        assert mock_adapter.recall.await_args.args[0].entity_names == ["Melon"]

    @pytest.mark.asyncio
    async def test_stopword_only_message_reaches_no_entity_recall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty hint set gates entity recall off entirely.

        Scope, stated precisely: this proves the *wiring* — that ``context.py`` honours
        an empty resolution rather than querying anyway. It does not prove the resolver
        rejects stopwords, because the resolver is mocked here; that guard is proven at
        its own altitude in
        ``test_service_entity_resolution.py::test_never_invents_a_name_the_graph_does_not_hold``.

        It is still a real regression guard: the removed heuristic returned ``["What"]``
        for this message, so reverting the wiring makes recall fire and this test fail.
        """
        monkeypatch.setattr(ctx_module.settings, "proactive_memory_enabled", False)
        intent = IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        )
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.resolve_message_entities = AsyncMock(return_value=[])

        result = await assemble_context(
            user_message="What should I cook tonight?",
            session_messages=[],
            intent=intent,
            memory_adapter=mock_adapter,
            trace_id="t-stopword",
            session_id="sess-1",
        )

        assert result.memory_context is None
        mock_adapter.recall.assert_not_called()


class TestStanceItemAuthorship:
    """FRE-1299: the item-builder units, tested directly against the private functions
    rather than through ``assemble_context`` -- ``TestStanceEnrichment`` and
    ``TestBehaviouralStanceInjection``'s integration-level tests fail on a clean
    ``origin/main`` checkout for a pre-existing, unrelated reason (confirmed via
    baseline diff before this ticket touched anything), so asserting the
    ``asserted_by``-threading behaviour at that layer would be un-provable independent
    of that breakage.
    """

    def test_stance_item_carries_asserted_by_through(self) -> None:
        items = ctx_module._stance_context_items(
            ["Python"],
            [{"target": "Python", "affect": "prefers over Java", "asserted_by": "user"}],
        )
        assert items[0]["asserted_by"] == "user"

    def test_stance_item_canonicalizes_agent_value(self) -> None:
        items = ctx_module._stance_context_items(
            ["Python"],
            [{"target": "Python", "affect": "prefers over Java", "asserted_by": "agent"}],
        )
        assert items[0]["asserted_by"] == "agent"

    def test_stance_item_defaults_to_agent_when_asserted_by_absent(self) -> None:
        """FRE-1299 AC-3: absence in the row must not pass through as absence in the item."""
        items = ctx_module._stance_context_items(
            ["Python"], [{"target": "Python", "affect": "prefers over Java"}]
        )
        assert items[0]["asserted_by"] == "agent"

    def test_stance_item_denies_off_vocabulary_asserted_by(self) -> None:
        items = ctx_module._stance_context_items(
            ["Python"],
            [{"target": "Python", "affect": "prefers over Java", "asserted_by": "superuser"}],
        )
        assert items[0]["asserted_by"] == "agent"

    def test_behavioural_stance_item_carries_asserted_by_through(self) -> None:
        items = ctx_module._behavioural_stance_context_items(
            [
                {
                    "target": "Artifact",
                    "affect": "prefers explicit request before creation",
                    "asserted_by": "user",
                }
            ]
        )
        assert items[0]["asserted_by"] == "user"

    def test_behavioural_stance_item_defaults_to_agent_when_absent(self) -> None:
        items = ctx_module._behavioural_stance_context_items(
            [{"target": "Artifact", "affect": "prefers explicit request before creation"}]
        )
        assert items[0]["asserted_by"] == "agent"


class TestStanceEnrichment:
    """ADR-0126 T1 (FRE-1015): every recalled entity gets its current stance pushed into
    memory_context, enrichment on a selection recall already made -- not a new relevance
    decision.
    """

    def _intent(self) -> IntentResult:
        return IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        )

    def _mock_adapter_with_entity(self, name: str = "Python") -> AsyncMock:
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.recall = AsyncMock(
            return_value=MagicMock(
                entities=[
                    {
                        "name": name,
                        "entity_type": "Technology",
                        "description": "a programming language",
                        "mention_count": 3,
                    }
                ],
                episodes=[],
                relevance_scores={},
            )
        )
        return mock_adapter

    @pytest.mark.asyncio
    async def test_stance_item_appended_for_recalled_entity(self) -> None:
        """ADR-0126 T2 (FRE-1017) note: with the behavioural-profile injector also live,
        get_current_stances is awaited *twice* per turn on an authenticated request --
        once here (T1, entity-gated) and once for the curated behavioural set (T2,
        unconditional). The mock's fixed return_value never matches a curated target
        name, so T2 contributes no items here; only the call-count assertion style
        needs to look at the specific T1-shaped call rather than "the one call".
        """
        mock_adapter = self._mock_adapter_with_entity("Python")
        mock_adapter.get_current_stances = AsyncMock(
            return_value=[{"target": "Python", "affect": "prefers over Java", "mastery": None}]
        )

        result = await assemble_context(
            user_message="Tell me about Python please",
            session_messages=[],
            intent=self._intent(),
            memory_adapter=mock_adapter,
            trace_id="t-stance",
            authenticated=True,
        )

        assert result.memory_context is not None
        stance_items = [m for m in result.memory_context if m.get("type") == "stance"]
        assert len(stance_items) == 1
        assert stance_items[0]["target"] == "Python"
        assert stance_items[0]["affect"] == "prefers over Java"
        topic_scoped_calls = [
            c for c in mock_adapter.get_current_stances.await_args_list if c.args[0] == ["Python"]
        ]
        assert len(topic_scoped_calls) == 1
        assert topic_scoped_calls[0].kwargs["authenticated"] is True

    @pytest.mark.asyncio
    async def test_no_entities_recalled_skips_stance_fetch(self) -> None:
        """ADR-0126 T2 (FRE-1017) note: T1's entity-gated hook correctly makes no call
        (no entities recalled), but the behavioural-profile injector calls
        get_current_stances unconditionally for the curated set on an authenticated
        request -- so the mock is awaited exactly once, for the curated targets, not
        zero times.
        """
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.get_current_stances = AsyncMock(return_value=[])

        result = await assemble_context(
            user_message="hello there",
            session_messages=[],
            intent=self._intent(),
            memory_adapter=mock_adapter,
            trace_id="t-none",
            authenticated=True,
        )

        assert result.memory_context is None
        mock_adapter.get_current_stances.assert_awaited_once_with(
            list(ctx_module.CURATED_BEHAVIOURAL_STANCE_TARGETS),
            trace_id="t-none",
            authenticated=True,
        )

    @pytest.mark.asyncio
    async def test_unauthenticated_skips_stance_fetch_even_with_entities(self) -> None:
        mock_adapter = self._mock_adapter_with_entity("Python")
        mock_adapter.get_current_stances = AsyncMock(return_value=[])

        result = await assemble_context(
            user_message="Tell me about Python please",
            session_messages=[],
            intent=self._intent(),
            memory_adapter=mock_adapter,
            trace_id="t-unauth",
            authenticated=False,
        )

        assert result.memory_context is not None
        assert all(m.get("type") != "stance" for m in result.memory_context)
        mock_adapter.get_current_stances.assert_not_called()

    @pytest.mark.asyncio
    async def test_stance_fetch_failure_preserves_original_memory_context(self) -> None:
        """Fail-closed: a stance-layer fault omits enrichment, never fails the turn."""
        mock_adapter = self._mock_adapter_with_entity("Python")
        mock_adapter.get_current_stances = AsyncMock(side_effect=RuntimeError("stance db down"))

        result = await assemble_context(
            user_message="Tell me about Python please",
            session_messages=[],
            intent=self._intent(),
            memory_adapter=mock_adapter,
            trace_id="t-fail",
            authenticated=True,
        )

        assert result.memory_context is not None
        assert len(result.memory_context) == 1
        assert result.memory_context[0]["type"] == "entity"
        assert all(m.get("type") != "stance" for m in result.memory_context)

    @pytest.mark.asyncio
    async def test_stance_order_follows_entity_order_not_query_return_order(self) -> None:
        """ADR-0126: enrichment must not become a second, unstated ranking decision --
        stances render in the same order their entities were recalled in, regardless of
        the order the batched query happens to return rows.
        """
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.recall = AsyncMock(
            return_value=MagicMock(
                entities=[
                    {"name": "Alpha", "entity_type": "Topic", "description": "first"},
                    {"name": "Beta", "entity_type": "Topic", "description": "second"},
                ],
                episodes=[],
                relevance_scores={},
            )
        )
        # Query returns Beta before Alpha -- out of entity-recall order.
        mock_adapter.get_current_stances = AsyncMock(
            return_value=[
                {"target": "Beta", "affect": "likes it", "mastery": None},
                {"target": "Alpha", "affect": "dislikes it", "mastery": None},
            ]
        )

        result = await assemble_context(
            user_message="Tell me about Alpha and Beta",
            session_messages=[],
            intent=self._intent(),
            memory_adapter=mock_adapter,
            trace_id="t-order",
            authenticated=True,
        )

        assert result.memory_context is not None
        stance_targets = [m["target"] for m in result.memory_context if m.get("type") == "stance"]
        assert stance_targets == ["Alpha", "Beta"]

    @pytest.mark.asyncio
    async def test_stance_without_matching_entity_is_dropped(self) -> None:
        """get_current_stances returning a target not in entity_names never happens in
        practice (targets are exactly what was requested), but the join is by-target so
        an unmatched row is simply absent rather than fabricating a phantom entity.
        """
        mock_adapter = self._mock_adapter_with_entity("Python")
        mock_adapter.get_current_stances = AsyncMock(
            return_value=[{"target": "SomethingElse", "affect": "x", "mastery": None}]
        )

        result = await assemble_context(
            user_message="Tell me about Python please",
            session_messages=[],
            intent=self._intent(),
            memory_adapter=mock_adapter,
            trace_id="t-unmatched",
            authenticated=True,
        )

        assert result.memory_context is not None
        assert all(m.get("type") != "stance" for m in result.memory_context)


class TestBehaviouralStanceInjection:
    """ADR-0126 T2 (FRE-1017): the curated standing-behavioural Stance set is pushed
    into context on every authenticated turn, independent of what the recall path
    selected -- unlike TestStanceEnrichment's topic-scoped push, this never reads
    memory_context for its targets.
    """

    def _intent(self) -> IntentResult:
        return IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        )

    @pytest.mark.asyncio
    async def test_curated_stances_appended_when_nothing_else_recalled(self) -> None:
        """The AC-2 scenario at the unit level: memory_context starts as None (no
        entities recalled at all) and the behavioural layer still injects.
        """
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.get_current_stances = AsyncMock(
            return_value=[
                {
                    "target": "Artifact",
                    "affect": "prefers explicit request before creation",
                    "mastery": None,
                },
                {
                    "target": "Plain text responses",
                    "affect": "prefers by default for follow-up data",
                    "mastery": None,
                },
            ]
        )

        result = await assemble_context(
            user_message="hello there",
            session_messages=[],
            intent=self._intent(),
            memory_adapter=mock_adapter,
            trace_id="t-behavioural",
            authenticated=True,
        )

        assert result.memory_context is not None
        behavioural_items = {
            m["target"]: m["affect"]
            for m in result.memory_context
            if m.get("type") == "behavioural_stance"
        }
        assert behavioural_items == {
            "Artifact": "prefers explicit request before creation",
            "Plain text responses": "prefers by default for follow-up data",
        }
        mock_adapter.get_current_stances.assert_awaited_once_with(
            list(ctx_module.CURATED_BEHAVIOURAL_STANCE_TARGETS),
            trace_id="t-behavioural",
            authenticated=True,
        )

    @pytest.mark.asyncio
    async def test_curated_items_appear_in_curated_order_not_query_return_order(
        self,
    ) -> None:
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        # Returned out of the curated tuple's own declared order.
        mock_adapter.get_current_stances = AsyncMock(
            return_value=[
                {"target": "Health Issues", "affect": "wants condition-level recall"},
                {"target": "Artifact", "affect": "prefers explicit request before creation"},
            ]
        )

        result = await assemble_context(
            user_message="hello there",
            session_messages=[],
            intent=self._intent(),
            memory_adapter=mock_adapter,
            trace_id="t-order",
            authenticated=True,
        )

        assert result.memory_context is not None
        behavioural_targets = [
            m["target"] for m in result.memory_context if m.get("type") == "behavioural_stance"
        ]
        curated = list(ctx_module.CURATED_BEHAVIOURAL_STANCE_TARGETS)
        expected = [t for t in curated if t in {"Health Issues", "Artifact"}]
        assert behavioural_targets == expected

    @pytest.mark.asyncio
    async def test_unauthenticated_skips_behavioural_fetch(self) -> None:
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.get_current_stances = AsyncMock(return_value=[])

        result = await assemble_context(
            user_message="hello there",
            session_messages=[],
            intent=self._intent(),
            memory_adapter=mock_adapter,
            trace_id="t-unauth",
            authenticated=False,
        )

        assert result.memory_context is None
        mock_adapter.get_current_stances.assert_not_called()

    @pytest.mark.asyncio
    async def test_behavioural_fetch_failure_preserves_memory_context(self) -> None:
        """Fail-closed: a stance-layer fault omits the layer for this turn, never
        fails the turn -- memory_context stays exactly what it was before this hook,
        including staying None.
        """
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.get_current_stances = AsyncMock(side_effect=RuntimeError("stance db down"))

        result = await assemble_context(
            user_message="hello there",
            session_messages=[],
            intent=self._intent(),
            memory_adapter=mock_adapter,
            trace_id="t-fail",
            authenticated=True,
        )

        assert result.memory_context is None

    @pytest.mark.asyncio
    async def test_curated_target_with_no_stance_is_absent(self) -> None:
        """get_current_stances returning fewer targets than requested is normal (not
        every curated target has a current stance) -- the join is by-target, so a
        missing one is simply absent rather than fabricated.
        """
        mock_adapter = AsyncMock()
        mock_adapter.is_connected = AsyncMock(return_value=True)
        mock_adapter.get_current_stances = AsyncMock(return_value=[])

        result = await assemble_context(
            user_message="hello there",
            session_messages=[],
            intent=self._intent(),
            memory_adapter=mock_adapter,
            trace_id="t-empty",
            authenticated=True,
        )

        assert result.memory_context is None


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
