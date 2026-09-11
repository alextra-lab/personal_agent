"""FRE-1479 — the reranker score reaches the admission boundary, and gates there (ADR-0148 D4).

Substrate-free: the arms, the reranker and the Cypher session are mocked, so these run under
``make test``.

The chain under test is four steps long, and the score was discarded at the first of them:
``_rerank_fused_items`` built ``{rr.index: rr.score}``, sorted by it and returned the original
``FusedResult`` objects, whose ``score`` field still held the RRF rank-fusion value. Each step
is pinned here, and then the whole chain is pinned end to end -- because a test that injects a
score at the boundary would pass while the plumbing stayed broken, which is exactly what
ADR-0148 AC-4 forbids ("the value the predicate compared is the value that path's scorer
produced").
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.captains_log.turn_evidence import DropReason
from personal_agent.config.settings import get_settings
from personal_agent.memory.fusion import FusedResult, MultiPathRecallResult, RankedResult
from personal_agent.memory.protocol import BroadRecallResult
from personal_agent.memory.reranker import RerankResult, _passthrough
from personal_agent.memory.service import MemoryService
from personal_agent.request_gateway.context import _format_broad_recall_context

#: The serving reranker, as ``config/models.yaml:541`` names it. The gate compares against the
#: calibrated artifact's component, so a score from anything else is not a relevance value.
SERVING = "rerank-2.5"

#: The fallback reranker (``config/models.yaml:557``). Real scores, different scale -- FRE-695:
#: "reranker score scales are arbitrary and not comparable across arms".
FALLBACK = "Qwen/Qwen3-Reranker-4B-mxfp8"


def _service() -> MemoryService:
    service = MemoryService()  # fre-375-allow: arms/rerank/session mocked; no substrate touched
    service.connected = True
    service.driver = object()  # truthy; never used when the reranker is off
    return service


def _entity_row(eid: str, name: str) -> dict[str, Any]:
    """One row in the shape ``_multipath_broad_entities``'s entity Cypher returns."""
    return {
        "id": eid,
        "name": name,
        "type": "Concept",
        "description": f"{name} description",
        "mentions": 1,
        "provenance_state": "asserted",
        "extractor_model": "m",
        "source_referents": [],
    }


def _service_with_entity_rows(rows: list[dict[str, Any]]) -> MemoryService:
    """A service whose entity-resolution Cypher returns ``rows`` and whose turn query is empty."""
    service = MemoryService.__new__(MemoryService)
    service.connected = True

    entity_result = AsyncMock()
    entity_result.data = AsyncMock(return_value=rows)
    empty_result = AsyncMock()
    empty_result.data = AsyncMock(return_value=[])

    async def _run(cypher: str, **kwargs: object) -> AsyncMock:
        return (
            entity_result if "MATCH (e:Entity) WHERE elementId(e) = eid" in cypher else empty_result
        )

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


def _broad(entities: list[dict[str, Any]]) -> BroadRecallResult:
    """Group entity dicts into a BroadRecallResult the way the protocol adapter does."""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for entity in entities:
        by_type.setdefault(str(entity.get("type", "Unknown")), []).append(entity)
    return BroadRecallResult(
        entities_by_type=by_type, recent_sessions=[], total_entity_count=len(entities)
    )


def _arm_bound(
    monkeypatch, bound: float | None, *, enabled: bool = True, calibrated: str | None = SERVING
) -> None:
    """Arm the gate at ``bound``, against a calibration naming ``calibrated``.

    The calibrated model is stubbed rather than read from a committed artifact so these
    stay tests of the gate. The loader that supplies it in production has its own tests in
    ``tests/personal_agent/config/test_broad_recall_bound_calibration.py``.
    """
    s = get_settings()
    monkeypatch.setattr(s, "broad_recall_relevance_bound", bound, raising=False)
    monkeypatch.setattr(s, "broad_recall_relevance_gate_enabled", enabled, raising=False)
    monkeypatch.setattr(
        "personal_agent.request_gateway.context._calibrated_reranker_model",
        lambda: calibrated,
    )


class TestScoreReachesTheBoundary:
    """AC-1: the reranker's own score survives every step to ``_format_broad_recall_context``."""

    @pytest.mark.asyncio
    async def test_rerank_fused_items_keeps_the_score_it_ordered_by(self, monkeypatch) -> None:
        """The core stops discarding the number it sorted on, and records what produced it."""
        monkeypatch.setattr(get_settings(), "reranker_enabled", True, raising=False)
        service = _service()
        service._resolve_item_texts = AsyncMock(return_value={"e1": "a", "e2": "b"})
        fused = [FusedResult("e1", 0.9, 2), FusedResult("e2", 0.4, 1)]

        async def _fake_rerank(**kwargs: object) -> list[RerankResult]:
            return [
                RerankResult(index=1, score=0.81, document="b", model_id=SERVING),
                RerankResult(index=0, score=0.12, document="a", model_id=SERVING),
            ]

        monkeypatch.setattr("personal_agent.memory.reranker.rerank", _fake_rerank)
        out = await service._rerank_fused_items("q", fused)

        by_id = {item.item_id: item for item in out}
        assert by_id["e2"].rerank_score == 0.81
        assert by_id["e1"].rerank_score == 0.12
        assert by_id["e2"].rerank_model == SERVING
        # The RRF value is untouched -- the two numbers are different facts.
        assert by_id["e1"].score == 0.9

    @pytest.mark.asyncio
    async def test_entity_payload_carries_the_reranker_score(self) -> None:
        """``_multipath_broad_entities`` puts the score on the dict the boundary reads."""
        service = _service_with_entity_rows([_entity_row("e1", "Kafka")])
        service._multipath_fused_recall = AsyncMock(
            return_value=MultiPathRecallResult(
                items=[FusedResult("e1", 0.5, 1, rerank_score=0.77, rerank_model=SERVING)],
                arms_executed=["lexical"],
                arms_failed=[],
                per_arm_counts={"lexical": 1},
                fused_set_size=1,
                path="broad",
            )
        )
        entities = await service._multipath_broad_entities(
            "q",
            limit=5,
            entity_types=None,
            user_id=None,
            authenticated=False,
            trace_id=None,
            session_id=None,
        )
        assert entities[0]["relevance_score"] == 0.77
        assert entities[0]["relevance_model"] == SERVING

    @pytest.mark.asyncio
    async def test_boundary_sees_the_score_the_core_computed(self, monkeypatch) -> None:
        """End to end: the value at the boundary is the one the reranker produced.

        Driven through the core, the resolver and the grouping the adapter performs -- never
        injected at the boundary, which is the substitute ADR-0148 AC-4 fails on.
        """
        monkeypatch.setattr(get_settings(), "reranker_enabled", True, raising=False)
        _arm_bound(monkeypatch, None)
        core = _service()
        core._resolve_item_texts = AsyncMock(return_value={"e1": "a", "e2": "b"})
        # Two distinct items on purpose: _rerank_fused_items returns early at
        # `len(items) <= 1`, so a one-item fused set is never scored at all.
        core.multi_query_recall_arm = AsyncMock(
            return_value=[RankedResult("e1", 1), RankedResult("e2", 2)]
        )
        core.lexical_recall_arm = AsyncMock(return_value=[RankedResult("e1", 1)])
        monkeypatch.setattr(get_settings(), "multiquery_arm_enabled", True, raising=False)
        monkeypatch.setattr(get_settings(), "lexical_arm_enabled", True, raising=False)
        monkeypatch.setattr(get_settings(), "structural_arm_enabled", False, raising=False)

        async def _fake_rerank(**kwargs: object) -> list[RerankResult]:
            return [
                RerankResult(index=0, score=0.64, document="a", model_id=SERVING),
                RerankResult(index=1, score=0.21, document="b", model_id=SERVING),
            ]

        monkeypatch.setattr("personal_agent.memory.reranker.rerank", _fake_rerank)
        recall = await core._multipath_fused_recall("q", path="broad")
        produced = next(i.rerank_score for i in recall.items if i.item_id == "e1")

        resolver = _service_with_entity_rows([_entity_row("e1", "Kafka")])
        resolver._multipath_fused_recall = AsyncMock(return_value=recall)
        entities = await resolver._multipath_broad_entities(
            "q",
            limit=5,
            entity_types=None,
            user_id=None,
            authenticated=False,
            trace_id=None,
            session_id=None,
        )
        items, scores, _ = _format_broad_recall_context(_broad(entities))

        assert produced == 0.64
        # The score reaches the boundary on the sibling map, never on the rendered item --
        # ADR-0148 D6: "a rendered item carries no score and no band" (FRE-1004's pattern).
        assert list(scores.values()) == [produced]
        assert "relevance_score" not in items[0]

    @pytest.mark.asyncio
    async def test_a_single_item_fused_set_is_never_scored(self, monkeypatch) -> None:
        """``_rerank_fused_items`` returns early at one item, so no relevance is established.

        One of ADR-0148 D4's named no-score conditions, and easy to miss because the item
        still arrives looking ordinary.
        """
        monkeypatch.setattr(get_settings(), "reranker_enabled", True, raising=False)
        service = _service()
        out = await service._rerank_fused_items("q", [FusedResult("e1", 0.9, 1)])
        assert out[0].rerank_score is None
        assert out[0].rerank_model is None

    @pytest.mark.asyncio
    async def test_the_gate_compares_the_value_the_core_produced(self, monkeypatch) -> None:
        """AC-4's provenance half: admission flips exactly at the reranker's own score.

        Bracketing the produced value is what proves the predicate compared *it* rather
        than a substitute. An equality assertion on a rendered field could not -- D6
        forbids rendering the score at all.
        """
        entity = _entity_row("e1", "Kafka") | {
            "relevance_score": 0.64,
            "relevance_model": SERVING,
        }
        _arm_bound(monkeypatch, 0.6399)
        admitted, _, _ = _format_broad_recall_context(_broad([entity]))
        _arm_bound(monkeypatch, 0.6401)
        rejected, _, discards = _format_broad_recall_context(_broad([entity]))

        assert len(admitted) == 1
        assert rejected == []
        assert [d[1] for d in discards] == [0.64]


class TestTheGateRejectsAtTheMeasuredNonMatch:
    """AC-2 and AC-3: the gate binds, and the test that proves it can fail."""

    @pytest.mark.asyncio
    async def test_candidate_at_the_bound_median_is_not_admitted(self, monkeypatch) -> None:
        """AC-2: a candidate below the calibrated bound does not enter memory_context."""
        _arm_bound(monkeypatch, 0.50)
        entity = _entity_row("e1", "Kafka") | {"relevance_score": 0.49, "relevance_model": SERVING}
        items, _, discards = _format_broad_recall_context(_broad([entity]))
        assert items == []
        assert [d[2] for d in discards] == [DropReason.RECALL_RELEVANCE_BOUND]

    @pytest.mark.asyncio
    async def test_the_same_candidate_is_admitted_with_the_gate_off(self, monkeypatch) -> None:
        """AC-3: with the gate disabled the fixture is admitted, so AC-2 can fail."""
        _arm_bound(monkeypatch, 0.50, enabled=False)
        entity = _entity_row("e1", "Kafka") | {"relevance_score": 0.49, "relevance_model": SERVING}
        items, _, discards = _format_broad_recall_context(_broad([entity]))
        assert len(items) == 1
        assert discards == ()

    @pytest.mark.asyncio
    async def test_a_candidate_at_the_bound_is_admitted(self, monkeypatch) -> None:
        """Below the bound is rejected -- the ``>=`` convention of memory/service.py:5020."""
        _arm_bound(monkeypatch, 0.50)
        entity = _entity_row("e1", "Kafka") | {"relevance_score": 0.50, "relevance_model": SERVING}
        items, _, _ = _format_broad_recall_context(_broad([entity]))
        assert len(items) == 1


class TestNoRelevanceValueNeverAdmitsOnOrder:
    """AC-4: the five conditions in which the path has no calibrated relevance value.

    ADR-0148 D4 names three (disabled, raising, empty). Two more exist in the code and are
    the dangerous ones: ``rerank()`` never raises -- it returns ``_passthrough``, whose scores
    are rank order -- and a successful fallback returns real scores from a different model.
    """

    @pytest.mark.asyncio
    async def test_passthrough_scores_are_not_relevance_values(self) -> None:
        """A degraded reranker fabricates 1/(i+1); that is rank order, not a measurement."""
        results = _passthrough(["a", "b"])
        assert [r.score for r in results] == [1.0, 0.5]
        assert all(r.model_id is None for r in results)

    @pytest.mark.asyncio
    async def test_unscored_item_is_not_admitted(self, monkeypatch) -> None:
        """Reranker disabled, or an item the response omitted: no score, no admission."""
        _arm_bound(monkeypatch, 0.50)
        entity = _entity_row("e1", "Kafka") | {"relevance_score": None, "relevance_model": None}
        items, _, discards = _format_broad_recall_context(_broad([entity]))
        assert items == []
        assert [d[2] for d in discards] == [DropReason.RECALL_RELEVANCE_UNAVAILABLE]

    @pytest.mark.asyncio
    async def test_fallback_model_score_is_not_admitted(self, monkeypatch) -> None:
        """A real Qwen score is not in the Voyage-calibrated space (FRE-695)."""
        _arm_bound(monkeypatch, 0.50)
        entity = _entity_row("e1", "Kafka") | {"relevance_score": 0.99, "relevance_model": FALLBACK}
        items, _, discards = _format_broad_recall_context(_broad([entity]))
        assert items == [], "a high score from an uncalibrated arm still admitted"
        assert [d[2] for d in discards] == [DropReason.RECALL_RELEVANCE_UNAVAILABLE]

    @pytest.mark.asyncio
    async def test_partial_response_admits_only_the_scored_item(self, monkeypatch) -> None:
        """One call can yield scored and unscored items: the predicate is per item."""
        _arm_bound(monkeypatch, 0.50)
        scored = _entity_row("e1", "Kafka") | {"relevance_score": 0.80, "relevance_model": SERVING}
        omitted = _entity_row("e2", "Knossos") | {"relevance_score": None, "relevance_model": None}
        items, _, discards = _format_broad_recall_context(_broad([scored, omitted]))
        assert [i["name"] for i in items] == ["Kafka"]
        assert [d[2] for d in discards] == [DropReason.RECALL_RELEVANCE_UNAVAILABLE]

    @pytest.mark.asyncio
    async def test_legacy_single_path_branch_carries_no_relevance_value(self, monkeypatch) -> None:
        """multipath_recall_enabled=False never reranks, so it never establishes relevance."""
        _arm_bound(monkeypatch, 0.50)
        # The legacy branch builds entity dicts with no relevance key at all.
        items, _, discards = _format_broad_recall_context(_broad([_entity_row("e1", "Kafka")]))
        assert items == []
        assert [d[2] for d in discards] == [DropReason.RECALL_RELEVANCE_UNAVAILABLE]

    @pytest.mark.asyncio
    async def test_unavailable_is_distinct_from_below_bound(self, monkeypatch) -> None:
        """The two rejections stay separable, which is AC-4's substance before FRE-1476."""
        _arm_bound(monkeypatch, 0.50)
        low = _entity_row("e1", "Kafka") | {"relevance_score": 0.10, "relevance_model": SERVING}
        none = _entity_row("e2", "Knossos") | {"relevance_score": None, "relevance_model": None}
        _, _, discards = _format_broad_recall_context(_broad([low, none]))
        assert {d[2] for d in discards} == {
            DropReason.RECALL_RELEVANCE_BOUND,
            DropReason.RECALL_RELEVANCE_UNAVAILABLE,
        }

    @pytest.mark.asyncio
    async def test_gate_is_inert_without_a_bound(self, monkeypatch) -> None:
        """A missing calibration never defaults to zero, and never gates (ADR-0148 D4)."""
        _arm_bound(monkeypatch, None)
        items, _, discards = _format_broad_recall_context(_broad([_entity_row("e1", "Kafka")]))
        assert len(items) == 1
        assert discards == ()


class TestSessionsAreUnchanged:
    """The reranker never scored a session summary row, and this ticket does not gate one."""

    @pytest.mark.asyncio
    async def test_sessions_survive_a_fully_rejected_entity_set(self, monkeypatch) -> None:
        _arm_bound(monkeypatch, 0.50)
        broad = BroadRecallResult(
            entities_by_type={"Concept": [_entity_row("e1", "Kafka")]},
            recent_sessions=[{"session_id": "s1", "session_summary": "a talk"}],
            total_entity_count=1,
        )
        items, _, _ = _format_broad_recall_context(broad)
        assert [i["type"] for i in items] == ["session"]


class TestTheCoreStillOrdersWithoutGating:
    """AC-5: ADR-0103 §4 and ADR-0104 AC-5 are untouched by carrying the score."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "rerank_results",
        [
            pytest.param(
                [
                    RerankResult(index=0, score=0.1, document="a", model_id=SERVING),
                    RerankResult(index=1, score=0.9, document="b", model_id=SERVING),
                ],
                id="fully-scored",
            ),
            pytest.param(
                [RerankResult(index=1, score=0.9, document="b", model_id=SERVING)],
                id="partially-scored",
            ),
            pytest.param([], id="empty-response"),
        ],
    )
    async def test_rerank_returns_a_permutation_never_a_subset(
        self, monkeypatch, rerank_results
    ) -> None:
        monkeypatch.setattr(get_settings(), "reranker_enabled", True, raising=False)
        service = _service()
        service._resolve_item_texts = AsyncMock(return_value={"e1": "a", "e2": "b"})
        fused = [FusedResult("e1", 0.9, 2), FusedResult("e2", 0.4, 1)]

        async def _fake_rerank(**kwargs: object) -> list[RerankResult]:
            return list(rerank_results)

        monkeypatch.setattr("personal_agent.memory.reranker.rerank", _fake_rerank)
        out = await service._rerank_fused_items("q", fused)

        assert len(out) == len(fused)
        assert {item.item_id for item in out} == {"e1", "e2"}
