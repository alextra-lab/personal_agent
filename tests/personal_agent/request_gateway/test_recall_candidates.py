"""Recall candidates survive budget trimming so the drop becomes recordable (FRE-1004).

ADR-0125 D3 item 5: ``apply_budget`` nulls ``memory_context`` outright, so without a
sibling record of what recall offered, a budget-dropped item is indistinguishable from an
item recall never found. These tests pin the sibling record — and pin that adding it
changes nothing the model sees.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.captains_log.turn_evidence import (
    CandidatePopulation,
    CandidateSource,
    DropReason,
    MemoryItemKind,
)
from personal_agent.memory.proactive_types import (
    ProactiveMemoryCandidate,
    ProactiveMemoryDiscard,
    ProactiveMemorySuggestions,
    ProactiveScoreComponents,
)
from personal_agent.request_gateway.budget import _trim_history, apply_budget
from personal_agent.request_gateway.context import assemble_context
from personal_agent.request_gateway.types import (
    AssembledContext,
    Complexity,
    IntentResult,
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


def _discard(name: str, score: float | None, reason: DropReason) -> ProactiveMemoryDiscard:
    return ProactiveMemoryDiscard(
        kind="entity",
        payload={"type": "entity", "name": name, "entity_type": "PERSON"},
        relevance_score=score,
        drop_reason=reason,
    )


class TestProactiveDiscardsReachTheRecord:
    """FRE-1060 — the candidates proactive's own gates removed are named, not absent.

    Before this, ``build_proactive_suggestions`` returned only survivors, so the record
    reported post-selection survivors as the population — five of twelve on the live melon
    turn, with seven discards no durable artifact named.
    """

    def _adapter(
        self,
        candidates: list[ProactiveMemoryCandidate],
        discarded: list[ProactiveMemoryDiscard],
    ) -> MagicMock:
        adapter = MagicMock()
        adapter.is_connected = AsyncMock(return_value=True)
        adapter.resolve_message_entities = AsyncMock(return_value=["Paris"])
        adapter.suggest_relevant = AsyncMock(
            return_value=ProactiveMemorySuggestions(candidates=candidates, discarded=discarded)
        )
        return adapter

    @pytest.mark.asyncio
    async def test_discards_are_recorded_with_their_gate(self, monkeypatch) -> None:
        """AC-1 and AC-2: each discard names the gate, and the two gates differ."""
        monkeypatch.setattr(
            "personal_agent.request_gateway.context.settings.proactive_memory_enabled",
            True,
            raising=False,
        )
        adapter = self._adapter(
            [_candidate("Paris", 0.82)],
            [
                _discard("Berlin", 0.61, DropReason.RECALL_ITEM_CAP),
                _discard("Lisbon", 0.55, DropReason.RECALL_CANDIDATE_CAP),
            ],
        )

        result = await assemble_context(
            user_message="tell me about Paris",
            session_messages=[],
            intent=_intent(),
            memory_adapter=adapter,
            trace_id="t",
        )

        by_id = {c.identity: c for c in result.recall_candidates}
        assert set(by_id) == {"Paris", "Berlin", "Lisbon"}
        assert by_id["Paris"].pre_drop_reason is None
        assert by_id["Berlin"].pre_drop_reason is DropReason.RECALL_ITEM_CAP
        assert by_id["Lisbon"].pre_drop_reason is DropReason.RECALL_CANDIDATE_CAP
        assert by_id["Berlin"].score == pytest.approx(0.61)

    @pytest.mark.asyncio
    async def test_discards_do_not_enter_memory_context(self, monkeypatch) -> None:
        """The record grows; what the model sees does not.

        A discarded candidate must never reach ``memory_context`` — that would make this
        ticket a recall change rather than a visibility change.
        """
        monkeypatch.setattr(
            "personal_agent.request_gateway.context.settings.proactive_memory_enabled",
            True,
            raising=False,
        )
        adapter = self._adapter(
            [_candidate("Paris", 0.82)],
            [_discard("Berlin", 0.61, DropReason.RECALL_ITEM_CAP)],
        )

        result = await assemble_context(
            user_message="tell me about Paris",
            session_messages=[],
            intent=_intent(),
            memory_adapter=adapter,
            trace_id="t",
        )

        assert result.memory_context is not None
        assert [m["name"] for m in result.memory_context] == ["Paris"]

    @pytest.mark.asyncio
    async def test_an_all_discarded_result_still_records_and_still_falls_through(
        self, monkeypatch
    ) -> None:
        """The seam codex caught: emitting nothing is when the discards matter most.

        ``context.py`` uses the proactive result only when it is non-empty and otherwise
        falls through to entity-match recall. Binding the discards inside that arm would
        lose them on exactly the turn whose record most needs them. Both properties are
        asserted together, because fixing one by breaking the other would turn a
        visibility change into a recall change.
        """
        from personal_agent.memory.protocol import MemoryRecallResult

        monkeypatch.setattr(
            "personal_agent.request_gateway.context.settings.proactive_memory_enabled",
            True,
            raising=False,
        )
        adapter = self._adapter(
            [],
            [
                _discard("Berlin", 0.29, DropReason.RECALL_SCORE_THRESHOLD),
                _discard("Lisbon", 0.11, DropReason.RECALL_SCORE_FLOOR),
            ],
        )
        adapter.recall = AsyncMock(
            return_value=MemoryRecallResult(
                episodes=[{"turn_id": "turn-7", "summary": "we discussed Paris"}],
                entities=[],
                relevance_scores={"turn-7": 0.91},
            )
        )

        result = await assemble_context(
            user_message="tell me about Paris",
            session_messages=[],
            intent=_intent(),
            memory_adapter=adapter,
            trace_id="t",
        )

        by_id = {c.identity: c for c in result.recall_candidates}
        # The discards survived the fall-through...
        assert by_id["Berlin"].pre_drop_reason is DropReason.RECALL_SCORE_THRESHOLD
        assert by_id["Lisbon"].pre_drop_reason is DropReason.RECALL_SCORE_FLOOR
        # ...and the fallback recall still ran, unchanged.
        adapter.recall.assert_awaited_once()
        assert by_id["turn-7"].pre_drop_reason is None
        assert result.memory_context is not None
        assert [m["conversation_id"] for m in result.memory_context] == ["turn-7"]
        # ...but the record must NOT claim completeness: the entity-match path it fell
        # through to truncates via entity_names[:5] and limit=5 without reporting it.
        assert result.candidate_population is CandidatePopulation.POST_SELECTION

    @pytest.mark.asyncio
    async def test_the_gateway_claims_a_complete_population(self, monkeypatch) -> None:
        """The record says which of the two things it is (FRE-1060 §2.5b)."""
        monkeypatch.setattr(
            "personal_agent.request_gateway.context.settings.proactive_memory_enabled",
            True,
            raising=False,
        )
        adapter = self._adapter([_candidate("Paris", 0.82)], [])

        result = await assemble_context(
            user_message="tell me about Paris",
            session_messages=[],
            intent=_intent(),
            memory_adapter=adapter,
            trace_id="t",
        )

        assert result.candidate_population is CandidatePopulation.OFFERED

    @pytest.mark.asyncio
    async def test_apply_budget_preserves_the_population_claim(self, monkeypatch) -> None:
        """Stage 7 rebuilds the context; the claim must not silently reset to the default.

        Losing it here would downgrade every live turn's record to "survivors only" while
        the completeness had in fact been established.
        """
        ctx = AssembledContext(
            messages=[{"role": "user", "content": "hi"}],
            memory_context=[{"type": "entity", "name": "Paris"}],
            tool_definitions=None,
            candidate_population=CandidatePopulation.OFFERED,
        )

        roomy = apply_budget(ctx, max_tokens=10_000, trace_id="t")
        # A tight budget makes Stage 7 actually trim, which is when the reconstruction
        # runs down its most lossy path — the claim has to survive that too.
        trimmed = apply_budget(ctx, max_tokens=1, trace_id="t")

        assert roomy.candidate_population is CandidatePopulation.OFFERED
        assert trimmed.candidate_population is CandidatePopulation.OFFERED
        assert trimmed.memory_context is None, "the tight case must really have trimmed"

    @pytest.mark.asyncio
    async def test_broad_recall_does_not_claim_a_complete_population(self, monkeypatch) -> None:
        """The over-claim code review confirmed: OFFERED was stamped unconditionally.

        `recall_broad` bounds its own read with `limit` and reports nothing about what that
        cut, so a record from this path names survivors only. Claiming `offered` for it
        would make the flag assert exactly the survivors-as-population reading it exists to
        prevent — and an analyst filtering on `offered`, as this diff's own docs instruct,
        would trust it.
        """
        from personal_agent.memory.protocol import BroadRecallResult

        adapter = MagicMock()
        adapter.is_connected = AsyncMock(return_value=True)
        adapter.recall_broad = AsyncMock(
            return_value=BroadRecallResult(
                entities_by_type={"LOCATION": [{"name": "Paris", "description": "capital"}]},
                recent_sessions=[],
                total_entity_count=1,
            )
        )

        result = await assemble_context(
            user_message="what do you remember about me?",
            session_messages=[],
            intent=_intent(TaskType.MEMORY_RECALL),
            memory_adapter=adapter,
            trace_id="t",
        )

        adapter.recall_broad.assert_awaited_once()
        assert result.candidate_population is CandidatePopulation.POST_SELECTION

    @pytest.mark.asyncio
    async def test_a_failure_after_the_gates_keeps_the_discards(self, monkeypatch) -> None:
        """The exception path must not convert named drops into "recall offered nothing".

        Proactive discards 12, then the entity-match fall-through raises — a Neo4j timeout,
        which the broad `except` exists to absorb. Returning `()` there recorded
        `state=EMPTY, candidate_count=0` for a turn that retrieved and gated a full
        population: the absence-vs-drop confusion this ticket closes, on the failure path,
        firing on exactly the turns most likely to be investigated.
        """
        monkeypatch.setattr(
            "personal_agent.request_gateway.context.settings.proactive_memory_enabled",
            True,
            raising=False,
        )
        adapter = self._adapter(
            [],
            [
                _discard("Berlin", 0.29, DropReason.RECALL_SCORE_THRESHOLD),
                _discard("Lisbon", 0.31, DropReason.RECALL_ITEM_CAP),
            ],
        )
        adapter.recall = AsyncMock(side_effect=RuntimeError("neo4j session dropped"))

        result = await assemble_context(
            user_message="tell me about Paris",
            session_messages=[],
            intent=_intent(),
            memory_adapter=adapter,
            trace_id="t",
        )

        by_id = {c.identity: c for c in result.recall_candidates}
        assert by_id["Berlin"].pre_drop_reason is DropReason.RECALL_SCORE_THRESHOLD
        assert by_id["Lisbon"].pre_drop_reason is DropReason.RECALL_ITEM_CAP
        assert result.memory_context is None, "the failure still degrades recall as before"
        assert result.candidate_population is CandidatePopulation.POST_SELECTION
