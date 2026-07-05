"""Unit tests for ADR-0100 relevance-bounded recall helpers (FRE-653).

These tests cover the pure helper functions extracted from ``query_memory`` —
relevance ranking (defect 3 fix), the vector similarity floor, and the
``memory_recall`` telemetry event builder. They require no Neo4j; the de-gated
candidate Cypher is exercised by the integration tests in
``test_memory_service.py`` and by the FRE-489 probe.
"""

from datetime import datetime, timedelta, timezone

import pytest

from personal_agent.config.settings import AppConfig
from personal_agent.memory.models import MemoryQuery, TurnNode
from personal_agent.memory.service import (
    _build_memory_recall_event,
    _filter_entities_by_floor,
    _filter_turns_by_hard_recency,
    _hard_recency_cutoff_iso,
    _rank_conversations_by_relevance,
)


def _turn(turn_id: str, *, days_ago: int = 0, text: str = "hello") -> TurnNode:
    return TurnNode(
        turn_id=turn_id,
        timestamp=datetime.now(timezone.utc) - timedelta(days=days_ago),
        user_message=text,
        key_entities=[],
    )


class TestRankConversationsByRelevance:
    """AC-2 mechanism: returned order is relevance-ranked, not timestamp-ordered."""

    def test_orders_by_score_descending(self) -> None:
        """A high-scoring old turn ranks ahead of a low-scoring recent turn."""
        old_relevant = _turn("old", days_ago=200)
        recent_weak = _turn("recent", days_ago=1)
        # Input order is timestamp-style (recent first); scores invert it.
        ranked = _rank_conversations_by_relevance(
            [recent_weak, old_relevant],
            {"old": 0.91, "recent": 0.12},
        )
        assert [c.turn_id for c in ranked] == ["old", "recent"]

    def test_missing_score_treated_as_zero(self) -> None:
        """A turn with no score sorts below a scored turn."""
        a = _turn("a")
        b = _turn("b")
        ranked = _rank_conversations_by_relevance([a, b], {"a": 0.5})
        assert [c.turn_id for c in ranked] == ["a", "b"]

    def test_stable_on_ties(self) -> None:
        """Equal scores preserve input order (stable sort)."""
        a = _turn("a")
        b = _turn("b")
        c = _turn("c")
        ranked = _rank_conversations_by_relevance([a, b, c], {"a": 0.5, "b": 0.5, "c": 0.5})
        assert [x.turn_id for x in ranked] == ["a", "b", "c"]

    def test_empty_input(self) -> None:
        """An empty candidate list returns empty."""
        assert _rank_conversations_by_relevance([], {}) == []


class TestFilterEntitiesByFloor:
    """AC-4 mechanism: vector-expanded candidates below the floor are dropped."""

    def test_drops_below_floor(self) -> None:
        """Entities below the floor are removed; survivors keep their scores."""
        vector_results = [
            {"name": "Strong", "score": 0.82},
            {"name": "Weak", "score": 0.18},
        ]
        names, scores = _filter_entities_by_floor(vector_results, floor=0.30)
        assert names == ["Strong"]
        assert scores == {"Strong": 0.82}

    def test_floor_zero_keeps_all(self) -> None:
        """A zero floor admits every vector-matched entity (legacy-equivalent)."""
        vector_results = [
            {"name": "A", "score": 0.5},
            {"name": "B", "score": 0.01},
        ]
        names, scores = _filter_entities_by_floor(vector_results, floor=0.0)
        assert set(names) == {"A", "B"}

    def test_all_below_floor_returns_empty(self) -> None:
        """When nothing clears the floor the candidate set is empty (AC-4)."""
        vector_results = [{"name": "A", "score": 0.1}, {"name": "B", "score": 0.2}]
        names, scores = _filter_entities_by_floor(vector_results, floor=0.5)
        assert names == []
        assert scores == {}

    def test_ignores_malformed_rows(self) -> None:
        """Rows missing a name or score are skipped without error."""
        vector_results = [{"name": "A", "score": 0.9}, {"score": 0.9}, {"name": "B"}]
        names, _ = _filter_entities_by_floor(vector_results, floor=0.3)
        assert names == ["A"]


class TestBuildMemoryRecallEvent:
    """AC-6: empty_result flag agrees with the actual payload."""

    def test_empty_result_true_on_empty_payload(self) -> None:
        """An empty payload sets empty_result True and zero counts."""
        event = _build_memory_recall_event(
            returned=[],
            candidate_set_size=0,
            vector_scores={},
            vector_entity_count=0,
            recall_latency_ms=3.0,
            similarity_floor=0.3,
            relevance_bounded_enabled=True,
        )
        assert event["empty_result"] is True
        assert event["result_count"] == 0
        assert event["candidate_set_size"] == 0

    def test_empty_result_false_on_nonempty_payload(self) -> None:
        """A non-empty payload sets empty_result False with the right counts."""
        returned = [_turn("a", text="alpha beta gamma"), _turn("b", text="delta")]
        event = _build_memory_recall_event(
            returned=returned,
            candidate_set_size=5,
            vector_scores={"E1": 0.8, "E2": 0.4, "E3": 0.6},
            vector_entity_count=3,
            recall_latency_ms=12.5,
            similarity_floor=0.3,
            relevance_bounded_enabled=True,
        )
        assert event["empty_result"] is False
        assert event["result_count"] == 2
        assert event["candidate_set_size"] == 5
        assert event["vector_entity_count"] == 3

    def test_vector_score_aggregates(self) -> None:
        """top/median vector scores are computed from the score map."""
        event = _build_memory_recall_event(
            returned=[_turn("a")],
            candidate_set_size=1,
            vector_scores={"E1": 0.8, "E2": 0.4, "E3": 0.6},
            vector_entity_count=3,
            recall_latency_ms=1.0,
            similarity_floor=0.0,
            relevance_bounded_enabled=False,
        )
        assert event["top_vector_score"] == 0.8
        assert event["median_vector_score"] == 0.6

    def test_no_vector_scores_defaults_zero(self) -> None:
        """With no vector scores both aggregates default to 0.0."""
        event = _build_memory_recall_event(
            returned=[_turn("a")],
            candidate_set_size=1,
            vector_scores={},
            vector_entity_count=0,
            recall_latency_ms=1.0,
            similarity_floor=0.0,
            relevance_bounded_enabled=False,
        )
        assert event["top_vector_score"] == 0.0
        assert event["median_vector_score"] == 0.0

    def test_recency_span_and_token_count(self) -> None:
        """Span spans oldest-to-newest hit; token count is positive."""
        returned = [
            _turn("old", days_ago=100, text="one two three four"),
            _turn("new", days_ago=0, text="five six"),
        ]
        event = _build_memory_recall_event(
            returned=returned,
            candidate_set_size=2,
            vector_scores={},
            vector_entity_count=0,
            recall_latency_ms=1.0,
            similarity_floor=0.0,
            relevance_bounded_enabled=True,
        )
        # ~100 days between oldest and newest hit.
        assert event["recency_span_seconds"] > 99 * 86400
        assert event["recalled_token_count"] > 0

    def test_single_result_span_zero(self) -> None:
        """A single returned turn has zero recency span."""
        event = _build_memory_recall_event(
            returned=[_turn("a")],
            candidate_set_size=1,
            vector_scores={},
            vector_entity_count=0,
            recall_latency_ms=1.0,
            similarity_floor=0.0,
            relevance_bounded_enabled=True,
        )
        assert event["recency_span_seconds"] == 0.0


class TestSettingsDefaults:
    """AC-7 posture: relevance-bounded recall is off by default, floor is 0.0."""

    def test_defaults_off_and_zero_floor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The flag defaults off and the floor defaults to 0.0 (legacy-equivalent).

        FRE-677: ``AppConfig`` reads ``os.environ`` via the ``AGENT_`` prefix, so
        the live ``.env`` flag state (loaded into the environment by any earlier
        test) would otherwise leak in and make this default-posture assertion
        order-dependent. Clear the two vars so we assert the model field defaults,
        not machine-specific config.
        """
        monkeypatch.delenv("AGENT_RELEVANCE_BOUNDED_RECALL_ENABLED", raising=False)
        monkeypatch.delenv("AGENT_RECALL_SIMILARITY_FLOOR", raising=False)
        cfg = AppConfig()
        assert cfg.relevance_bounded_recall_enabled is False
        assert cfg.recall_similarity_floor == 0.0


class TestHardRecencyCutoffIso:
    """FRE-658: explicit hard time window re-applied on the relevance-bounded path."""

    def test_returns_naive_iso_when_set(self) -> None:
        """A set hard_recency_days yields a NAIVE ISO cutoff (matches legacy L2442)."""
        cutoff = _hard_recency_cutoff_iso(MemoryQuery(entity_names=["X"], hard_recency_days=7))
        assert cutoff is not None
        # Must be naive (no UTC offset) so it string-compares against the naive
        # datetime.utcnow().isoformat() timestamps the legacy cutoff uses.
        assert "+00:00" not in cutoff
        assert datetime.fromisoformat(cutoff).tzinfo is None

    def test_none_when_unset(self) -> None:
        """No explicit window -> None (automatic path stays de-gated, AC-1a)."""
        assert _hard_recency_cutoff_iso(MemoryQuery(entity_names=["X"])) is None

    def test_none_when_zero(self) -> None:
        """hard_recency_days == 0 is not a window -> None."""
        assert (
            _hard_recency_cutoff_iso(MemoryQuery(entity_names=["X"], hard_recency_days=0)) is None
        )


class TestFilterTurnsByHardRecency:
    """FRE-658: hard post-recall window filter for the de-gated multi-path path."""

    def test_drops_out_of_window(self) -> None:
        """Turns older than the window are dropped; in-window turns kept."""
        kept = _filter_turns_by_hard_recency(
            [_turn("recent", days_ago=2), _turn("old", days_ago=100)], 7
        )
        assert [t.turn_id for t in kept] == ["recent"]

    def test_none_is_noop(self) -> None:
        """hard_recency_days None returns all turns unchanged (AC-1a invariance)."""
        kept = _filter_turns_by_hard_recency(
            [_turn("recent", days_ago=2), _turn("old", days_ago=100)], None
        )
        assert [t.turn_id for t in kept] == ["recent", "old"]

    def test_zero_is_noop(self) -> None:
        """hard_recency_days 0 is not a window -> no filtering."""
        kept = _filter_turns_by_hard_recency([_turn("old", days_ago=100)], 0)
        assert [t.turn_id for t in kept] == ["old"]

    def test_naive_timestamp_normalised_not_crash(self) -> None:
        """A naive turn timestamp is treated as UTC (no aware/naive compare crash)."""
        naive = TurnNode(
            turn_id="naive",
            timestamp=datetime.utcnow(),
            user_message="x",
            key_entities=[],
        )
        kept = _filter_turns_by_hard_recency([naive], 7)
        assert [t.turn_id for t in kept] == ["naive"]
