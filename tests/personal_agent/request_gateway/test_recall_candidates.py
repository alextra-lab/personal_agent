"""Recall candidates survive budget trimming so the drop becomes recordable (FRE-1004).

ADR-0125 D3 item 5: ``apply_budget`` nulls ``memory_context`` outright, so without a
sibling record of what recall offered, a budget-dropped item is indistinguishable from an
item recall never found. These tests pin the sibling record — and pin that adding it
changes nothing the model sees.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.captains_log.turn_evidence import CandidateSource, MemoryItemKind
from personal_agent.memory.proactive_types import (
    ProactiveMemoryCandidate,
    ProactiveMemorySuggestions,
    ProactiveScoreComponents,
)
from personal_agent.request_gateway.budget import _trim_history, apply_budget
from personal_agent.request_gateway.context import assemble_context
from personal_agent.request_gateway.types import (
    AssembledContext,
    Complexity,
    IntentResult,
    RecallCandidate,
    RecallResult,
    TaskType,
)


def _intent(task_type: TaskType = TaskType.CONVERSATIONAL) -> IntentResult:
    return IntentResult(
        task_type=task_type,
        complexity=Complexity.SIMPLE,
        confidence=0.9,
        signals=[],
    )


def _proactive_adapter(candidates: list[ProactiveMemoryCandidate]) -> MagicMock:
    adapter = MagicMock()
    adapter.is_connected = AsyncMock(return_value=True)
    # FRE-1041: the graph-anchored entity hint replaced the capitalisation heuristic,
    # so the adapter is asked which entities the message names.
    adapter.resolve_message_entities = AsyncMock(return_value=["Paris"])
    adapter.suggest_relevant = AsyncMock(
        return_value=ProactiveMemorySuggestions(candidates=candidates)
    )
    return adapter


def _candidate(name: str, score: float) -> ProactiveMemoryCandidate:
    return ProactiveMemoryCandidate(
        kind="entity",
        payload={
            "type": "entity",
            "name": name,
            "entity_type": "PERSON",
            "description": "d",
            "mention_count": 1,
        },
        relevance_score=score,
        score_components=ProactiveScoreComponents(
            embedding=score, entity_overlap=0.0, recency=0.0, topic_coherence=0.0
        ),
    )


class TestStageSixEmitsCandidates:
    @pytest.mark.asyncio
    async def test_proactive_scores_reach_the_candidate_record(self, monkeypatch) -> None:
        """context.py discarded relevance_score entirely before FRE-1004."""
        monkeypatch.setattr(
            "personal_agent.request_gateway.context.settings.proactive_memory_enabled",
            True,
            raising=False,
        )
        adapter = _proactive_adapter([_candidate("Paris", 0.82), _candidate("Berlin", 0.41)])

        result = await assemble_context(
            user_message="tell me about Paris",
            session_messages=[],
            intent=_intent(),
            memory_adapter=adapter,
            trace_id="t",
        )

        by_id = {c.identity: c for c in result.recall_candidates}
        assert set(by_id) == {"Paris", "Berlin"}
        assert by_id["Paris"].score == pytest.approx(0.82)
        assert by_id["Berlin"].score == pytest.approx(0.41)
        assert by_id["Paris"].kind is MemoryItemKind.ENTITY
        assert by_id["Paris"].source is CandidateSource.MEMORY_CONTEXT

    @pytest.mark.asyncio
    async def test_no_memory_adapter_yields_no_candidates(self) -> None:
        result = await assemble_context(
            user_message="hello",
            session_messages=[],
            intent=_intent(),
            memory_adapter=None,
            trace_id="t",
        )
        assert result.recall_candidates == ()

    @pytest.mark.asyncio
    async def test_session_fact_candidates_are_recorded(self, monkeypatch) -> None:
        """context.py injects recall-controller facts bypassing memory_context."""
        recall = RecallResult(
            reclassified=True,
            original_task_type=TaskType.CONVERSATIONAL,
            trigger_cue="that thing",
            candidates=[
                RecallCandidate(
                    fact="Primary database is PostgreSQL",
                    source_turn=3,
                    noun_phrase="database",
                    confidence=0.77,
                )
            ],
        )

        result = await assemble_context(
            user_message="what was it again?",
            session_messages=[],
            intent=_intent(),
            memory_adapter=None,
            trace_id="t",
            recall_context=recall,
        )

        facts = [
            c for c in result.recall_candidates if c.source is CandidateSource.SESSION_FACT_SECTION
        ]
        assert len(facts) == 1
        assert facts[0].identity == "turn:3"
        assert facts[0].kind is MemoryItemKind.SESSION_FACT
        assert facts[0].score == pytest.approx(0.77)


class TestBudgetPreservesCandidates:
    def _ctx(self, memory_context: list[dict]) -> AssembledContext:
        from personal_agent.captains_log.turn_evidence import build_recall_candidates

        return AssembledContext(
            messages=[
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "old " * 400},
                {"role": "assistant", "content": "reply " * 400},
                {"role": "user", "content": "now"},
            ],
            memory_context=memory_context,
            tool_definitions=None,
            recall_candidates=build_recall_candidates(memory_context, {}),
        )

    def test_candidates_survive_the_memory_drop(self) -> None:
        """The drop is only recordable because the candidate outlives it."""
        memory = [{"type": "entity", "name": n, "description": "d " * 200} for n in ("A", "B")]
        trimmed = apply_budget(self._ctx(memory), max_tokens=20, trace_id="t", session_id="s")

        assert trimmed.memory_context is None
        assert trimmed.overflow_action in {"dropped_memory_context", "dropped_tool_definitions"}
        assert {c.identity for c in trimmed.recall_candidates} == {"A", "B"}

    def test_candidates_do_not_change_the_token_count(self) -> None:
        """Sibling metadata must never become model-visible or budget-visible."""
        memory = [{"type": "entity", "name": "A", "description": "d"}]
        with_candidates = apply_budget(
            self._ctx(memory), max_tokens=100_000, trace_id="t", session_id="s"
        )
        without = apply_budget(
            AssembledContext(
                messages=list(self._ctx(memory).messages),
                memory_context=memory,
                tool_definitions=None,
            ),
            max_tokens=100_000,
            trace_id="t",
            session_id="s",
        )
        assert with_candidates.token_count == without.token_count

    def test_trim_history_preserves_system_messages(self) -> None:
        """The invariant session-fact admission rests on (they ride a system message)."""
        messages = [
            {"role": "system", "content": "state doc"},
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old reply"},
            {"role": "system", "content": "## Session Fact Recall"},
            {"role": "user", "content": "now"},
        ]
        trimmed, did_trim = _trim_history(messages)

        assert did_trim is True
        assert [m["content"] for m in trimmed if m["role"] == "system"] == [
            "state doc",
            "## Session Fact Recall",
        ]


class TestDefaultEntityMatchPath:
    """The path taken when proactive_memory_enabled is False — the settings default.

    Before FRE-1004 this branch dropped the episode's turn_id and discarded the
    relevance scores the recall result already carried, so on the default configuration
    every episode candidate was anonymous and unscored — the two properties AC-3 asks
    for. These tests pin both.
    """

    def _adapter(self) -> MagicMock:
        from personal_agent.memory.protocol import MemoryRecallResult

        adapter = MagicMock()
        adapter.is_connected = AsyncMock(return_value=True)
        # FRE-1041: this path is now gated on graph-anchored entity resolution rather
        # than the capitalisation heuristic, so the hint must be supplied here.
        adapter.resolve_message_entities = AsyncMock(return_value=["Paris"])
        adapter.recall = AsyncMock(
            return_value=MemoryRecallResult(
                episodes=[
                    {"turn_id": "turn-7", "summary": "we discussed Paris", "key_entities": []},
                    {"turn_id": "turn-9", "summary": "we discussed Berlin", "key_entities": []},
                ],
                entities=[{"name": "Paris", "entity_type": "LOCATION", "description": "capital"}],
                relevance_scores={"turn-7": 0.91, "turn-9": 0.42},
            )
        )
        return adapter

    @pytest.mark.asyncio
    async def test_episodes_carry_their_identity(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "personal_agent.request_gateway.context.settings.proactive_memory_enabled",
            False,
            raising=False,
        )
        result = await assemble_context(
            user_message="tell me about Paris",
            session_messages=[],
            intent=_intent(),
            memory_adapter=self._adapter(),
            trace_id="t",
        )

        identities = {c.identity for c in result.recall_candidates}
        assert "turn-7" in identities
        assert "turn-9" in identities
        assert "" not in identities, "an anonymous candidate cannot be joined to anything"

    @pytest.mark.asyncio
    async def test_episode_scores_are_not_discarded(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "personal_agent.request_gateway.context.settings.proactive_memory_enabled",
            False,
            raising=False,
        )
        result = await assemble_context(
            user_message="tell me about Paris",
            session_messages=[],
            intent=_intent(),
            memory_adapter=self._adapter(),
            trace_id="t",
        )

        by_id = {c.identity: c.score for c in result.recall_candidates}
        assert by_id["turn-7"] == pytest.approx(0.91)
        assert by_id["turn-9"] == pytest.approx(0.42)

    @pytest.mark.asyncio
    async def test_identity_agrees_with_the_renderer(self, monkeypatch) -> None:
        """The candidate identity and the executor's rendered identity must match.

        The executor derives rendered identities from the same memory_context dicts via
        memory_item_identity. If the two disagreed, an admitted item would be recorded
        as dropped.
        """
        from personal_agent.captains_log.turn_evidence import memory_item_identity

        monkeypatch.setattr(
            "personal_agent.request_gateway.context.settings.proactive_memory_enabled",
            False,
            raising=False,
        )
        result = await assemble_context(
            user_message="tell me about Paris",
            session_messages=[],
            intent=_intent(),
            memory_adapter=self._adapter(),
            trace_id="t",
        )

        assert result.memory_context is not None
        from_dicts = [memory_item_identity(m)[1] for m in result.memory_context]
        assert from_dicts == [c.identity for c in result.recall_candidates]
