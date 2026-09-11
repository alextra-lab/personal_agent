"""FRE-1480 — the entity-match path acquires a relevance value, and gates on it (ADR-0148 D4).

Substrate-free: the arms, the reranker and the Cypher session are mocked, so these run under
``make test``.

The sibling of ``test_broad_recall_relevance.py``, and deliberately built the same way. The
chain under test is four steps long and the score was discarded at the third:
``_multipath_fused_recall`` computes it, ``_rerank_fused_items`` preserves it onto the
``FusedResult`` (FRE-1479), and then ``_multipath_query_memory`` replaced it with
``(total - position) / total`` — rank order wearing the score field. Every step is pinned
here, and then the whole chain end to end, because a test that injects a score at the
boundary would pass while the plumbing stayed broken. That is what ADR-0148 AC-4 forbids and
what this ticket's AC-5 restates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.captains_log.turn_evidence import DropReason
from personal_agent.config.settings import get_settings
from personal_agent.memory.fusion import FusedResult, MultiPathRecallResult, RankedResult
from personal_agent.memory.models import MemoryQuery
from personal_agent.memory.protocol import EntityResolutionResult
from personal_agent.memory.reranker import RerankResult
from personal_agent.memory.service import MemoryService
from personal_agent.request_gateway.context import RELEVANCE_UNAVAILABLE_CAUSE
from personal_agent.request_gateway.memory_status import MemoryStatus

#: The serving reranker, as ``config/models.yaml`` names it.
SERVING = "rerank-2.5"

#: The fallback reranker. Real scores, a different scale — FRE-695: reranker scales are
#: "arbitrary and not comparable across arms", so the bound describes neither the other.
FALLBACK = "Qwen/Qwen3-Reranker-4B-mxfp8"


def _core() -> MemoryService:
    """A service whose arms and reranker are mocked; nothing touches a substrate."""
    service = MemoryService()  # fre-375-allow: arms/rerank/session mocked; no substrate touched
    service.connected = True
    service.driver = object()  # truthy; never used when the arms are mocked
    return service


def _resolver(
    entity_rows: list[dict[str, Any]] | None = None,
    turn_rows: list[list[Any]] | None = None,
) -> MemoryService:
    """A service whose ``_resolve_fused_turns`` Cypher returns the given rows."""
    service = MemoryService.__new__(MemoryService)
    service.connected = True

    entity_result = AsyncMock()
    entity_result.values = AsyncMock(return_value=entity_rows or [])
    turn_result = AsyncMock()
    turn_result.values = AsyncMock(return_value=turn_rows or [])

    async def _run(cypher: str, **kwargs: object) -> AsyncMock:
        return entity_result if "MATCH (e:Entity)" in cypher else turn_result

    session = AsyncMock()
    session.run = AsyncMock(side_effect=_run)
    service.driver = MagicMock()
    service.driver.session = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=session),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    return service


def _entity_node_row(element_id: str, name: str) -> list[Any]:
    """One row in the shape ``_resolve_fused_turns``'s entity Cypher returns."""
    return [
        element_id,
        {
            "name": name,
            "entity_type": "Concept",
            "description": f"{name} description",
            "mention_count": 1,
        },
        [],
    ]


class TestTheScoreReachesTheBoundary:
    """AC-5: the reranker's own score survives ``_multipath_query_memory``."""

    @pytest.mark.asyncio
    async def test_entity_match_result_carries_the_reranker_score(self) -> None:
        """The value the core computed reaches ``MemoryQueryResult``, keyed and attributed.

        Before FRE-1480 this map held ``(total - position) / total`` for turns and nothing
        at all for entities, so the boundary had only rank order to admit on.
        """
        service = _resolver(entity_rows=[_entity_node_row("e-elem-1", "Kafka")])
        service._multipath_fused_recall = AsyncMock(
            return_value=MultiPathRecallResult(
                items=[
                    FusedResult(
                        "e-elem-1", 0.5, 1, kind="entity", rerank_score=0.77, rerank_model=SERVING
                    )
                ],
                arms_executed=["lexical"],
                arms_failed=[],
                per_arm_counts={"lexical": 1},
                fused_set_size=1,
                path="entity",
            )
        )
        result = await service._multipath_query_memory(
            MemoryQuery(limit=5),
            "q",
            access_context=None,
            trace_id=None,
            session_id=None,
            user_id=None,
            authenticated=False,
        )
        value = result.relevance_values["entity:Kafka"]
        assert value.score == 0.77
        assert value.model == SERVING
        assert result.relevance_scored is True

    @pytest.mark.asyncio
    async def test_the_key_is_namespaced_by_kind(self) -> None:
        """A turn id equal to an entity name must not take the entity's score.

        ``EntityNode.entity_id`` is populated from ``node.name`` (``service.py``) while a
        turn's identity is its ``turn_id``. The two spaces are not disjoint, so an
        unnamespaced map lets one kind silently overwrite the other's provenance.
        """
        collide = "Kafka"
        service = _resolver(
            entity_rows=[_entity_node_row("e-elem-1", collide)],
            turn_rows=[[{"turn_id": collide, "user_message": "m", "timestamp": None}]],
        )
        service._turn_node_from_node = lambda node: _turn_stub(collide)
        service._multipath_fused_recall = AsyncMock(
            return_value=MultiPathRecallResult(
                items=[
                    FusedResult(
                        collide, 0.9, 1, kind="turn", rerank_score=0.91, rerank_model=SERVING
                    ),
                    FusedResult(
                        "e-elem-1", 0.5, 1, kind="entity", rerank_score=0.42, rerank_model=SERVING
                    ),
                ],
                arms_executed=["lexical"],
                arms_failed=[],
                per_arm_counts={"lexical": 2},
                fused_set_size=2,
                path="entity",
            )
        )
        result = await service._multipath_query_memory(
            MemoryQuery(limit=5),
            "q",
            access_context=None,
            trace_id=None,
            session_id=None,
            user_id=None,
            authenticated=False,
        )
        assert result.relevance_values[f"turn:{collide}"].score == 0.91
        assert result.relevance_values[f"entity:{collide}"].score == 0.42

    @pytest.mark.asyncio
    async def test_boundary_sees_the_score_the_core_computed(self, monkeypatch) -> None:
        """End to end: the value at the boundary is the one the reranker produced.

        Driven through the real core rather than injected, which is the substitute AC-5
        fails on.
        """
        monkeypatch.setattr(get_settings(), "reranker_enabled", True, raising=False)
        monkeypatch.setattr(get_settings(), "multiquery_arm_enabled", True, raising=False)
        monkeypatch.setattr(get_settings(), "lexical_arm_enabled", True, raising=False)
        monkeypatch.setattr(get_settings(), "structural_arm_enabled", False, raising=False)
        core = _core()
        core._resolve_item_texts = AsyncMock(return_value={"e-elem-1": "a", "e-elem-2": "b"})
        core._multi_query_recall_arm_strict = AsyncMock(
            return_value=[RankedResult("e-elem-1", 1), RankedResult("e-elem-2", 2)]
        )
        core._lexical_recall_arm_strict = AsyncMock(return_value=[RankedResult("e-elem-1", 1)])

        async def _fake_rerank(**kwargs: object) -> list[RerankResult]:
            return [
                RerankResult(index=0, score=0.64, document="a", model_id=SERVING),
                RerankResult(index=1, score=0.21, document="b", model_id=SERVING),
            ]

        monkeypatch.setattr("personal_agent.memory.reranker.rerank", _fake_rerank)
        recall = await core._multipath_fused_recall("q", path="entity")
        produced = next(i.rerank_score for i in recall.items if i.item_id == "e-elem-1")

        resolver = _resolver(entity_rows=[_entity_node_row("e-elem-1", "Kafka")])
        resolver._multipath_fused_recall = AsyncMock(return_value=recall)
        result = await resolver._multipath_query_memory(
            MemoryQuery(limit=5),
            "q",
            access_context=None,
            trace_id=None,
            session_id=None,
            user_id=None,
            authenticated=False,
        )

        assert produced == 0.64
        assert result.relevance_values["entity:Kafka"].score == produced

    @pytest.mark.asyncio
    async def test_rank_order_is_not_a_relevance_value(self) -> None:
        """An item the reranker never scored contributes no relevance value.

        The defect this ticket closes: the path used to synthesise
        ``(total - position) / total`` for every turn and hand it on as a score.
        """
        service = _resolver(entity_rows=[_entity_node_row("e-elem-1", "Kafka")])
        service._multipath_fused_recall = AsyncMock(
            return_value=MultiPathRecallResult(
                items=[FusedResult("e-elem-1", 0.5, 1, kind="entity")],
                arms_executed=["lexical"],
                arms_failed=[],
                per_arm_counts={"lexical": 1},
                fused_set_size=1,
                path="entity",
            )
        )
        result = await service._multipath_query_memory(
            MemoryQuery(limit=5),
            "q",
            access_context=None,
            trace_id=None,
            session_id=None,
            user_id=None,
            authenticated=False,
        )
        assert result.relevance_values == {}


def _turn_stub(turn_id: str) -> Any:
    """A minimal TurnNode carrying every field the model requires.

    ``timestamp`` is not optional on ``TurnNode``; omitting it makes
    ``_resolve_fused_turns`` raise, and the raise is swallowed into ``resolve_failed``,
    so the test would fail for the wrong reason.
    """
    from personal_agent.memory.models import TurnNode

    return TurnNode(
        turn_id=turn_id,
        user_message="m",
        assistant_response="r",
        timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


#: The calibrated median top-ranked non-match, read from the committed artifact rather than
#: written here. ADR-0148 AC-10 makes the artifact the source of the configured value, and a
#: hand-copied constant would let the test and the gate drift apart silently.
def _calibration() -> Any:
    from personal_agent.config.calibration import (
        ENTITY_MATCH_RELEVANCE_BOUND_FILE,
        load_reranker_calibration,
        repository_root,
    )

    calibration = load_reranker_calibration(repository_root(), ENTITY_MATCH_RELEVANCE_BOUND_FILE)
    assert calibration is not None, "the committed entity-match calibration must exist (AC-7)"
    return calibration


async def _drive(
    monkeypatch,
    *,
    entities: list[dict[str, Any]] | None = None,
    episodes: list[dict[str, Any]] | None = None,
    relevance_scored: bool = True,
    gate_enabled: bool = True,
    bound: float | None = 0.338891,
    calibrated: str | None = SERVING,
) -> Any:
    """Drive the entity-match arm of ``assemble_context`` and return the assembled context.

    Goes through ``_query_memory_for_intent``'s real entity-match arm -- resolution,
    admission, the discard report and the status composition -- rather than calling the
    gate directly, because AC-6 is about the *composed* status and a direct call cannot
    observe it.
    """
    from personal_agent.config import settings as live_settings
    from personal_agent.memory.protocol import MemoryRecallResult
    from personal_agent.request_gateway.context import assemble_context
    from personal_agent.request_gateway.types import Complexity, IntentResult, TaskType

    monkeypatch.setattr(live_settings, "proactive_memory_enabled", False, raising=False)
    monkeypatch.setattr(live_settings, "entity_match_relevance_bound", bound, raising=False)
    monkeypatch.setattr(
        live_settings, "entity_match_relevance_gate_enabled", gate_enabled, raising=False
    )
    monkeypatch.setattr(
        "personal_agent.request_gateway.context._calibrated_reranker_model",
        lambda _filename=None: calibrated,
    )

    adapter = MagicMock()
    adapter.is_connected = AsyncMock(return_value=True)
    adapter.resolve_message_entities = AsyncMock(
        return_value=EntityResolutionResult(names=["Kafka"])
    )
    adapter.get_current_stances = AsyncMock(return_value=[])
    adapter.recall = AsyncMock(
        return_value=MemoryRecallResult(
            episodes=episodes or [],
            entities=entities or [],
            relevance_scores={},
            relevance_scored=relevance_scored,
        )
    )
    return await assemble_context(
        user_message="tell me about Kafka",
        session_messages=[],
        intent=IntentResult(
            task_type=TaskType.ANALYSIS,
            complexity=Complexity.SIMPLE,
            confidence=1.0,
            signals=[],
        ),
        memory_adapter=adapter,
        trace_id="t",
        authenticated=True,
    )


def _entity(score: float | None, model: str | None = SERVING) -> dict[str, Any]:
    """One entity payload as the adapter builds it, scored or not."""
    payload: dict[str, Any] = {
        "entity_id": "Kafka",
        "name": "Kafka",
        "entity_type": "Concept",
        "description": "a distributed log",
        "mention_count": 1,
    }
    if score is not None:
        payload["relevance_score"] = score
    if model is not None and score is not None:
        payload["relevance_model"] = model
    return payload


class TestTheGateRejectsAtTheMeasuredNonMatch:
    """AC-3 and AC-4: the gate binds at the boundary, and the test that proves it can fail."""

    @pytest.mark.asyncio
    async def test_candidate_at_the_calibrated_non_match_is_not_admitted(self, monkeypatch) -> None:
        """AC-3: a name that resolves, inside the 30-day window, still fails the bound.

        This is the case the path admits today -- name resolution plus recency with no
        relevance evidence at all.
        """
        median = _calibration().negative_median_rerank
        result = await _drive(monkeypatch, entities=[_entity(median)])
        assert result.memory_context is None

    @pytest.mark.asyncio
    async def test_the_same_candidate_is_admitted_with_the_gate_off(self, monkeypatch) -> None:
        """AC-4: with the gate disabled the identical fixture is admitted, so AC-3 can fail."""
        median = _calibration().negative_median_rerank
        result = await _drive(monkeypatch, entities=[_entity(median)], gate_enabled=False)
        assert result.memory_context is not None
        assert [i["type"] for i in result.memory_context] == ["entity"]

    @pytest.mark.asyncio
    async def test_a_candidate_at_the_bound_is_admitted(self, monkeypatch) -> None:
        """Below the bound is rejected -- the ``>=`` convention of memory/service.py:5020."""
        bound = _calibration().bound
        result = await _drive(monkeypatch, entities=[_entity(bound)], bound=bound)
        assert result.memory_context is not None

    @pytest.mark.asyncio
    async def test_every_admitted_item_carries_a_relevance_value(self, monkeypatch) -> None:
        """AC-2: admission implies a value, because an unscored item is dropped."""
        result = await _drive(
            monkeypatch,
            entities=[_entity(0.80), _entity(None) | {"name": "Unscored", "entity_id": "Unscored"}],
        )
        assert result.memory_context is not None
        assert [i["name"] for i in result.memory_context] == ["Kafka"]
        # AC-5: the value the record carries is the scorer's own, on the sibling map.
        assert {c.identity: c.score for c in result.recall_candidates}["Kafka"] == pytest.approx(
            0.80
        )
        assert "relevance_score" not in result.memory_context[0]


class TestNoRelevanceValueIsUnavailableNotAbsence:
    """AC-6: a path that established no relevance value may not claim absence.

    ADR-0148 D2 states the precedence as "fixed and total": UNAVAILABLE outranks
    everything, "whatever else happened", and "a partially degraded recall is therefore
    UNAVAILABLE, never NOTHING_RELEVANT".
    """

    @pytest.mark.asyncio
    async def test_an_unscored_item_is_dropped_and_the_turn_is_unavailable(
        self, monkeypatch
    ) -> None:
        """The reranker disabled, raising, or returning nothing -- all arrive as no score."""
        result = await _drive(monkeypatch, entities=[_entity(None)])
        assert result.memory_context is None
        assert result.memory_status.status is MemoryStatus.UNAVAILABLE
        assert result.memory_status.recall.cause == RELEVANCE_UNAVAILABLE_CAUSE

    @pytest.mark.asyncio
    async def test_a_fallback_model_score_is_not_a_relevance_value(self, monkeypatch) -> None:
        """A different reranker's real score sits on a scale this bound does not describe."""
        result = await _drive(monkeypatch, entities=[_entity(0.80, model=FALLBACK)])
        assert result.memory_context is None
        assert result.memory_status.status is MemoryStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_a_partial_response_is_unavailable_even_though_one_item_was_admitted(
        self, monkeypatch
    ) -> None:
        """The case a weaker predicate got wrong (codex plan-review).

        ``_rerank_fused_items`` can score some indices and omit others from one response.
        Admitting the scored item and reporting a completed run would present partial
        evidence as though the corpus had been searched to completion.
        """
        result = await _drive(
            monkeypatch,
            entities=[
                _entity(0.80),
                _entity(None) | {"name": "Unscored", "entity_id": "Unscored"},
            ],
        )
        assert result.memory_context is not None, "the scored item is still shown"
        assert result.memory_status.status is MemoryStatus.UNAVAILABLE
        assert result.memory_status.recall.cause == RELEVANCE_UNAVAILABLE_CAUSE

    @pytest.mark.asyncio
    async def test_every_candidate_measured_and_below_the_bound_is_nothing_relevant(
        self, monkeypatch
    ) -> None:
        """The companion. Relevance *was* established, and nothing cleared it.

        Without this, the test above could pass for the wrong reason -- a rule that
        reported UNAVAILABLE for every empty result would make absence unreachable, which
        is the defect ADR-0148 exists to remove rather than one to introduce.
        """
        median = _calibration().negative_median_rerank
        result = await _drive(monkeypatch, entities=[_entity(median)])
        assert result.memory_context is None
        assert result.memory_status.status is MemoryStatus.NOTHING_RELEVANT

    @pytest.mark.asyncio
    async def test_an_unreranked_result_is_admitted_but_unavailable(self, monkeypatch) -> None:
        """The legacy single-path branch: the bound does not apply, and neither does absence.

        Two questions, and FRE-1479 answered only the first. The bound was measured on
        reranker scores and describes only a reranked set, so gating here would reject
        every item on a number that never saw them. But the path established no relevance
        value either, and D4's no-score rule is unconditional -- the only fallback, a dense
        similarity, is the arm FRE-1477 measured as admitting no calibrated bound.
        """
        result = await _drive(monkeypatch, entities=[_entity(None)], relevance_scored=False)
        assert result.memory_context is not None, "the bound does not apply to an unreranked set"
        assert result.memory_status.status is MemoryStatus.UNAVAILABLE
        assert result.memory_status.recall.cause == RELEVANCE_UNAVAILABLE_CAUSE


class TestGenuinelyRelevantRecallSurvives:
    """AC-8: the bound does not suppress real relevance, measured two ways."""

    def test_the_committed_bound_admits_both_document_halves(self) -> None:
        """Each shape separately, so neither can drag the other above 90%."""
        calibration = _calibration()
        assert calibration.bound is not None
        for label, scores in (
            ("entity", calibration.entity_positive_scores),
            ("turn", calibration.turn_positive_scores),
        ):
            admitted = sum(1 for s in scores if s >= calibration.bound) / len(scores)
            assert admitted >= 0.90, f"{label}-document positives admitted {admitted:.1%}"

    @pytest.mark.asyncio
    async def test_per_probe_survival_end_to_end(self, monkeypatch) -> None:
        """Per probe, not in aggregate, through the whole admission chain.

        ADR-0148's own AC-6 states the reason: "An aggregate count that holds while
        individual probes swap outcomes is a failure, not a pass." The scores are the
        committed artifact's own labelled positives, so the population is the measured one.
        """
        calibration = _calibration()
        assert calibration.bound is not None
        positives = calibration.positive_scores

        admitted_before: list[str] = []
        admitted_after: list[str] = []
        for index, score in enumerate(positives):
            probe = f"probe-{index}"
            entity = _entity(score) | {"name": probe, "entity_id": probe}
            off = await _drive(monkeypatch, entities=[entity], gate_enabled=False)
            on = await _drive(monkeypatch, entities=[entity], bound=calibration.bound)
            if off.memory_context:
                admitted_before.append(probe)
            if on.memory_context:
                admitted_after.append(probe)

        assert len(admitted_before) == len(positives), "every positive is admitted gate-off"
        lost = set(admitted_before) - set(admitted_after)
        survived = len(admitted_after) / len(admitted_before)
        assert survived >= 0.90, f"only {survived:.1%} survived; lost {sorted(lost)[:5]}"
