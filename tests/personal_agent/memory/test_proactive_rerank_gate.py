"""FRE-1545 — the proactive path gates admission on the reranker score (ADR-0148 D4).

FRE-1477 found that the serving embedder cannot separate relevant from irrelevant on this
path at D4's rate, so recency could still carry an irrelevant item over the 0.30 bar. The
reranker separates where the embedder does not. These tests pin the gate against the
committed calibration (``config/calibration/proactive_rerank_relevance_bound.json``) and the
deployed defaults, rather than restating a number.

Every fixture has zero entity overlap and zero topic hits -- ADR-0148 D4's proactive clause
binds exactly those candidates -- unless a test says otherwise.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import personal_agent.memory.proactive as proactive_mod
from personal_agent.captains_log.turn_evidence import DropReason
from personal_agent.config.calibration import (
    PROACTIVE_RERANK_RELEVANCE_BOUND_FILE,
    RerankerRelevanceCalibration,
    load_relevance_calibration,
    load_reranker_calibration,
)
from personal_agent.config.config_guard import repo_root
from personal_agent.memory.models import RelevanceValue
from personal_agent.memory.proactive import (
    build_proactive_suggestions,
    score_proactive_relevance,
)
from personal_agent.memory.proactive_types import ProactiveMemoryDiscard, ProactiveMemorySuggestions
from personal_agent.memory.protocol import EntityResolutionResult
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter
from personal_agent.memory.reranker import RerankResult
from personal_agent.request_gateway.context import (
    RELEVANCE_UNAVAILABLE_CAUSE,
    _query_memory_for_intent,
)
from personal_agent.request_gateway.memory_status import RecallOutcome
from personal_agent.request_gateway.types import Complexity, IntentResult, TaskType

FALLBACK_MODEL = "Qwen/Qwen3-Reranker-4B-mxfp8"


@pytest.fixture(scope="module")
def calibration() -> RerankerRelevanceCalibration:
    """The committed proactive reranker calibration."""
    loaded = load_reranker_calibration(repo_root(), PROACTIVE_RERANK_RELEVANCE_BOUND_FILE)
    assert loaded is not None
    return loaded


@pytest.fixture(scope="module")
def embedder_non_match() -> float:
    """FRE-1477's measured median top-ranked non-match, in Neo4j score space.

    The vector score at which recency alone carried a candidate over the bar -- the
    situation this gate exists for.
    """
    loaded = load_relevance_calibration(repo_root())
    assert loaded is not None
    return loaded.negative_median_neo4j


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entity_row(
    name: str = "Kubernetes",
    description: str | None = "Container orchestration platform.",
    *,
    vector_score: float = 0.68,
    timestamp: str | None = None,
    measured: bool = True,
) -> dict[str, Any]:
    """A raw proactive row naming one entity and no turn."""
    return {
        "name": name,
        "entity_type": "Technology",
        "description": description,
        "vector_score": vector_score,
        "vector_score_measured": measured,
        "turn_id": None,
        "timestamp_iso": timestamp or _now(),
        "user_message": None,
        "summary": None,
        "key_entities": [],
        "mention_count": 3,
    }


def _value(score: float, model: str) -> RelevanceValue:
    return RelevanceValue(score=score, model=model)


def _build(
    rows: list[dict[str, Any]],
    relevance: dict[tuple[str, str], RelevanceValue] | None,
    session_entities: set[str] | None = None,
) -> ProactiveMemorySuggestions:
    return build_proactive_suggestions(
        rows, session_entities or set(), None, "t-fre1545", None, relevance=relevance
    )


def _reasons(out: ProactiveMemorySuggestions) -> list[DropReason]:
    return [d.drop_reason for d in out.discarded]


def _fake_rerank(scores: Sequence[float], model: str | None = "rerank-2.5") -> AsyncMock:
    """A ``rerank`` stand-in returning ``scores`` by input index, sorted descending."""

    async def _call(*, query: str, documents: Sequence[str], **_: Any) -> list[RerankResult]:
        results = [
            RerankResult(index=i, score=s, document=documents[i], model_id=model)
            for i, s in enumerate(scores)
        ]
        return sorted(results, key=lambda r: r.score, reverse=True)

    return AsyncMock(side_effect=_call)


class TestTheDeployedConfiguration:
    def test_the_gate_is_armed_at_the_calibrated_bound(
        self, calibration: RerankerRelevanceCalibration
    ) -> None:
        assert proactive_mod.settings.proactive_memory_rerank_relevance_bound == calibration.bound
        assert proactive_mod.settings.proactive_memory_relevance_gate_enabled is True
        assert proactive_mod.rerank_gate_armed() is True


class TestAC2TheGateRejectsAnIrrelevantRecentItem:
    """A candidate at the calibrated median top-ranked non-match, recent, with no evidence."""

    def _fixture(
        self, calibration: RerankerRelevanceCalibration, embedder_non_match: float
    ) -> tuple[list[dict[str, Any]], dict[tuple[str, str], RelevanceValue]]:
        rows = [_entity_row(vector_score=embedder_non_match, timestamp=_now())]
        relevance = {
            ("entity", "Kubernetes"): _value(
                calibration.negative_median_rerank, calibration.component.model
            )
        }
        return rows, relevance

    def test_rejected_by_the_reranker_bound(
        self, calibration: RerankerRelevanceCalibration, embedder_non_match: float
    ) -> None:
        rows, relevance = self._fixture(calibration, embedder_non_match)
        out = _build(rows, relevance)
        assert out.candidates == []
        assert _reasons(out) == [DropReason.RECALL_RELEVANCE_BOUND]

    def test_the_same_fixture_with_the_gate_disabled_is_admitted(
        self,
        calibration: RerankerRelevanceCalibration,
        embedder_non_match: float,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The control: recency does carry this candidate over the bar without the gate."""
        monkeypatch.setattr(
            proactive_mod.settings, "proactive_memory_relevance_gate_enabled", False
        )
        rows, relevance = self._fixture(calibration, embedder_non_match)
        out = _build(rows, relevance)
        assert [c.payload["name"] for c in out.candidates] == ["Kubernetes"]
        assert (
            out.candidates[0].relevance_score >= proactive_mod.settings.proactive_memory_min_score
        )

    def test_a_candidate_at_the_bound_is_admitted(
        self, calibration: RerankerRelevanceCalibration, embedder_non_match: float
    ) -> None:
        """Below the bound is rejected; at the bound is kept (the `>=` convention)."""
        assert calibration.bound is not None
        rows = [_entity_row(vector_score=embedder_non_match)]
        relevance = {("entity", "Kubernetes"): _value(calibration.bound, "rerank-2.5")}
        assert [c.payload["name"] for c in _build(rows, relevance).candidates] == ["Kubernetes"]


class TestAC3TheGatedValueIsTheRerankersOwnOutput:
    @pytest.mark.asyncio
    async def test_each_score_lands_on_its_own_candidate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real call returns results sorted by score, not by input order."""
        fake = _fake_rerank([0.10, 0.90])
        monkeypatch.setattr(proactive_mod, "rerank", fake)
        rows = [_entity_row("Alpha", "first"), _entity_row("Beta", "second")]

        values = await score_proactive_relevance(rows, "a query", trace_id="t")

        assert values == {
            ("entity", "Alpha"): _value(0.10, "rerank-2.5"),
            ("entity", "Beta"): _value(0.90, "rerank-2.5"),
        }
        kwargs = fake.await_args.kwargs
        assert kwargs["documents"] == ["Alpha first", "Beta second"]
        assert kwargs["top_k"] == 2  # every candidate is scored, not reranker_top_k

    @pytest.mark.asyncio
    async def test_the_gate_admits_by_those_scores(
        self, calibration: RerankerRelevanceCalibration, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert calibration.bound is not None
        below, above = calibration.bound - 0.05, calibration.bound + 0.05
        monkeypatch.setattr(proactive_mod, "rerank", _fake_rerank([above, below]))
        rows = [_entity_row("Alpha", "first"), _entity_row("Beta", "second")]

        values = await score_proactive_relevance(rows, "a query", trace_id="t")
        out = _build(rows, values)

        assert [c.payload["name"] for c in out.candidates] == ["Alpha"]
        assert [(d.payload["name"], d.drop_reason) for d in out.discarded] == [
            ("Beta", DropReason.RECALL_RELEVANCE_BOUND)
        ]

    @pytest.mark.asyncio
    async def test_a_passthrough_score_is_not_a_relevance_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rank order wearing the score field (model None) is never gated on."""
        monkeypatch.setattr(proactive_mod, "rerank", _fake_rerank([1.0], model=None))
        rows = [_entity_row()]
        values = await score_proactive_relevance(rows, "a query", trace_id="t")
        assert values == {}
        assert _reasons(_build(rows, values)) == [DropReason.RECALL_RELEVANCE_UNAVAILABLE]

    def test_a_fallback_model_score_is_unavailable_not_admitted(self) -> None:
        """A real score on another model's scale says nothing against this bound."""
        out = _build([_entity_row()], {("entity", "Kubernetes"): _value(0.99, FALLBACK_MODEL)})
        assert out.candidates == []
        assert _reasons(out) == [DropReason.RECALL_RELEVANCE_UNAVAILABLE]


class TestAC4NoScoreNoAdmissionOnOrder:
    @pytest.mark.asyncio
    async def test_reranker_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The real ``rerank()`` degrades to a passthrough; nothing is admitted on it."""
        monkeypatch.setattr(proactive_mod.settings, "reranker_enabled", False)
        rows = [_entity_row("Alpha", "first"), _entity_row("Beta", "second")]

        values = await score_proactive_relevance(rows, "a query", trace_id="t")
        out = _build(rows, values)

        assert values == {}
        assert out.candidates == []
        assert _reasons(out) == [DropReason.RECALL_RELEVANCE_UNAVAILABLE] * 2

    @pytest.mark.asyncio
    async def test_reranker_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            proactive_mod, "rerank", AsyncMock(side_effect=RuntimeError("voyage down"))
        )
        rows = [_entity_row("Alpha", "first"), _entity_row("Beta", "second")]

        values = await score_proactive_relevance(rows, "a query", trace_id="t")
        out = _build(rows, values)

        assert values == {}
        assert out.candidates == []
        assert _reasons(out) == [DropReason.RECALL_RELEVANCE_UNAVAILABLE] * 2

    def test_no_mapping_at_all_fails_safe(self) -> None:
        """A caller that forgets the mapping gets UNAVAILABLE, never admission on order."""
        out = _build([_entity_row()], None)
        assert _reasons(out) == [DropReason.RECALL_RELEVANCE_UNAVAILABLE]

    def test_a_candidate_with_entity_overlap_is_not_gated(self) -> None:
        """ADR-0148 D4's proactive clause binds only candidates with no other evidence."""
        out = _build([_entity_row()], None, session_entities={"Kubernetes"})
        assert [c.payload["name"] for c in out.candidates] == ["Kubernetes"]

    @pytest.mark.asyncio
    async def test_the_adapter_threads_the_scores_into_selection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The production caller computes the scores and passes them on (wiring)."""
        service = MagicMock()
        service.fetch_session_discussed_entity_names = AsyncMock(return_value=[])
        service.suggest_proactive_raw = AsyncMock(
            return_value=[_entity_row("Alpha", "first"), _entity_row("Beta", "second")]
        )

        async def fake_embed(*_a: object, **_k: object) -> list[float]:
            return [0.1, 0.2]

        monkeypatch.setattr("personal_agent.memory.protocol_adapter.generate_embedding", fake_embed)
        monkeypatch.setattr(proactive_mod, "rerank", _fake_rerank([0.9, 0.1]))

        result = await MemoryServiceAdapter(service=service).suggest_relevant(
            user_message="a query",
            session_entity_names=[],
            session_topic_hint=None,
            current_session_id="s1",
            trace_id="t1",
        )

        assert [c.payload["name"] for c in result.candidates] == ["Alpha"]
        assert [(d.payload["name"], d.drop_reason) for d in result.discarded] == [
            ("Beta", DropReason.RECALL_RELEVANCE_BOUND)
        ]


def _intent() -> IntentResult:
    return IntentResult(
        task_type=TaskType.CONVERSATIONAL, complexity=Complexity.SIMPLE, confidence=0.9, signals=[]
    )


def _discard(reason: DropReason, name: str = "Kubernetes") -> ProactiveMemoryDiscard:
    return ProactiveMemoryDiscard(
        kind="entity",
        payload={"type": "entity", "name": name, "description": "d"},
        relevance_score=None,
        drop_reason=reason,
    )


class TestAC4TheTurnSaysUnavailableNotNothingRelevant:
    """``_query_memory_for_intent`` composes the stage report the turn status reads."""

    async def _stage(
        self, monkeypatch: pytest.MonkeyPatch, suggestions: ProactiveMemorySuggestions
    ) -> tuple[RecallOutcome, str | None]:
        monkeypatch.setattr(
            "personal_agent.request_gateway.context.settings.proactive_memory_enabled", True
        )
        adapter = MagicMock()
        adapter.is_connected = AsyncMock(return_value=True)
        adapter.resolve_message_entities = AsyncMock(return_value=EntityResolutionResult(names=[]))
        adapter.suggest_relevant = AsyncMock(return_value=suggestions)
        _, _, _, stage = await _query_memory_for_intent(
            _intent(), "a query", adapter, "t", "s1", []
        )
        return stage.outcome, stage.cause

    @pytest.mark.asyncio
    async def test_a_fully_discarded_unavailable_result_is_not_nothing_relevant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        suggestions = ProactiveMemorySuggestions(
            discarded=[_discard(DropReason.RECALL_RELEVANCE_UNAVAILABLE)]
        )
        outcome, cause = await self._stage(monkeypatch, suggestions)
        assert outcome is RecallOutcome.FAILED
        assert cause == RELEVANCE_UNAVAILABLE_CAUSE

    @pytest.mark.asyncio
    async def test_the_control_a_measured_rejection_keeps_absence_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Measured and below the bound is a real absence; only unmeasured is UNAVAILABLE."""
        suggestions = ProactiveMemorySuggestions(
            discarded=[_discard(DropReason.RECALL_RELEVANCE_BOUND)]
        )
        outcome, cause = await self._stage(monkeypatch, suggestions)
        assert outcome is RecallOutcome.COMPLETED
        assert cause is None


class TestAC6TheRejectionIsSeparable:
    def test_a_reranker_rejection_is_not_a_score_threshold_rejection(
        self, calibration: RerankerRelevanceCalibration
    ) -> None:
        """An old, orthogonal candidate fails min_score too; the gate's reason still wins."""
        old = "2020-01-01T00:00:00+00:00"
        rows = [_entity_row(vector_score=0.5, timestamp=old)]
        relevance = {
            ("entity", "Kubernetes"): _value(calibration.negative_median_rerank, "rerank-2.5")
        }
        out = _build(rows, relevance)
        assert _reasons(out) == [DropReason.RECALL_RELEVANCE_BOUND]
        assert DropReason.RECALL_SCORE_THRESHOLD not in _reasons(out)

    def test_the_control_without_the_gate_it_is_a_threshold_rejection(
        self, calibration: RerankerRelevanceCalibration, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            proactive_mod.settings, "proactive_memory_relevance_gate_enabled", False
        )
        rows = [_entity_row(vector_score=0.5, timestamp="2020-01-01T00:00:00+00:00")]
        relevance = {
            ("entity", "Kubernetes"): _value(calibration.negative_median_rerank, "rerank-2.5")
        }
        assert _reasons(_build(rows, relevance)) == [DropReason.RECALL_SCORE_THRESHOLD]


def _episode_row(
    turn_id: str, *, summary: str | None, user_message: str | None, name: str = "Kubernetes"
) -> dict[str, Any]:
    row = _entity_row(name)
    row.update(
        {
            "turn_id": turn_id,
            "summary": summary,
            "user_message": user_message,
            "key_entities": [name],
        }
    )
    return row


class TestTheDocuments:
    """What the scorer sends -- identical to ``MemoryService._resolve_item_texts``."""

    async def _documents(
        self, rows: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> list[str]:
        fake = _fake_rerank([0.5] * 10)
        monkeypatch.setattr(proactive_mod, "rerank", fake)
        await score_proactive_relevance(rows, "a query", trace_id="t")
        return list(fake.await_args.kwargs["documents"]) if fake.await_count else []

    @pytest.mark.asyncio
    async def test_entity_and_episode_documents(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Entity ``name + ' ' + description``; episode ``coalesce(summary, user_message)``."""
        rows = [_episode_row("turn-1", summary=None, user_message="asked about pods")]
        assert await self._documents(rows, monkeypatch) == [
            "Kubernetes Container orchestration platform.",
            "asked about pods",
        ]

    @pytest.mark.asyncio
    async def test_an_empty_summary_is_kept_as_coalesce_keeps_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``coalesce`` stops at an empty string; an empty document is then not sent."""
        rows = [_episode_row("turn-1", summary="", user_message="asked about pods")]
        documents = await self._documents(rows, monkeypatch)
        assert documents == ["Kubernetes Container orchestration platform."]

    @pytest.mark.asyncio
    async def test_an_empty_episode_document_is_unscored_and_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [_episode_row("turn-1", summary=None, user_message="")]
        monkeypatch.setattr(proactive_mod, "rerank", _fake_rerank([0.9]))
        values = await score_proactive_relevance(rows, "a query", trace_id="t")
        out = _build(rows, values)
        assert ("episode", "turn-1") not in values
        assert {(d.kind, d.drop_reason) for d in out.discarded if d.kind == "episode"} == {
            ("episode", DropReason.RECALL_RELEVANCE_UNAVAILABLE)
        }

    @pytest.mark.asyncio
    async def test_duplicate_identities_are_sent_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two rows sharing an entity and a turn yield one document per identity."""
        rows = [
            _episode_row("turn-1", summary="pods", user_message=None),
            _episode_row("turn-1", summary="pods", user_message=None),
        ]
        assert await self._documents(rows, monkeypatch) == [
            "Kubernetes Container orchestration platform.",
            "pods",
        ]

    @pytest.mark.asyncio
    async def test_a_lexical_row_is_scored_like_any_other(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lexical augment's constant vector score does not matter to the reranker."""
        rows = [_entity_row("Lexical", "found by name", vector_score=0.3, measured=False)]
        assert await self._documents(rows, monkeypatch) == ["Lexical found by name"]

    @pytest.mark.asyncio
    async def test_an_empty_description_entity_is_not_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [_entity_row("Bare", None), _entity_row("Full", "has text")]
        assert await self._documents(rows, monkeypatch) == ["Full has text"]

    @pytest.mark.asyncio
    async def test_no_call_when_the_gate_is_unarmed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(proactive_mod.settings, "proactive_memory_rerank_relevance_bound", None)
        assert await self._documents([_entity_row()], monkeypatch) == []
