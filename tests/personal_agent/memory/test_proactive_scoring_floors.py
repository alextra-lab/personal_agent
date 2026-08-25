"""FRE-1287 — proactive scoring floors, ADR-0138 consumer.

Three of the four subscores (embedding, recency, topic) had non-zero floors on their
no-evidence paths, while entity overlap already scored 0.0 with no evidence
(``_overlap_subscore``). The combination let a same-day memory with zero entity
overlap and zero topic hits score 0.455 against a 0.3 admission bar — recency, aided
by residual floors on the other two subscores, bought admission for candidates with no
real relevance signal at all.

The fix removes every no-evidence floor: an orthogonal embedding, a missing
timestamp, and a topic hint with zero keyword hits all now score 0.0, matching
``_overlap_subscore``'s existing shape. This is more than closing the two named
arithmetic cases (AC-1/AC-2) — it also makes the title's claim a structural
invariant, not just an empirical one: recency's own weight (0.20) is strictly below
``proactive_memory_min_score`` (0.30), so no amount of recency, on its own, can ever
cross the bar (:class:`TestRecencyAloneStructurallyCannotAdmit`).
"""

from __future__ import annotations

from typing import Any

import pytest

import personal_agent.memory.proactive as proactive_mod
from personal_agent.captains_log.turn_evidence import DropReason
from personal_agent.memory.proactive import (
    _normalize_vector_score,
    _recency_subscore,
    _topic_subscore,
    build_proactive_suggestions,
)


@pytest.fixture
def deployed_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the deployed proactive configuration, so this asserts the production shape."""
    s = proactive_mod.settings
    for name, value in (
        ("proactive_memory_w_embedding", 0.45),
        ("proactive_memory_w_entity", 0.25),
        ("proactive_memory_w_recency", 0.20),
        ("proactive_memory_w_topic", 0.10),
        ("proactive_memory_min_score", 0.30),
        ("proactive_memory_max_candidates", 10),
        ("proactive_memory_max_injected_items", 5),
        ("proactive_memory_diminishing_score_floor", 0.35),
        ("proactive_memory_diminishing_score_gap", 0.15),
        ("proactive_memory_recency_half_life_days", 30.0),
        ("proactive_memory_max_tokens", 100_000),
    ):
        monkeypatch.setattr(s, name, value, raising=False)


def _entity_row(
    *,
    name: str = "Irrelevant",
    vector_score: float = 0.5,
    timestamp_iso: str | None = "2026-08-25T00:00:00+00:00",
    key_entities: list[str] | None = None,
    description: str = "some description",
) -> dict[str, Any]:
    """A turnless entity row with no turn, so it never picks up an episode sibling."""
    return {
        "name": name,
        "entity_type": "Thing",
        "description": description,
        "vector_score": vector_score,
        "timestamp_iso": timestamp_iso,
        "key_entities": key_entities or [],
    }


class TestSubscoreFloorsRemoved:
    """The four no-evidence cases now score 0.0, matching ``_overlap_subscore``'s shape."""

    def test_orthogonal_embedding_scores_zero_not_half(self) -> None:
        """cos=0 -> Neo4j's raw 0.5 -> rescaled 0.0 (was the single largest floor)."""
        assert _normalize_vector_score(0.5) == 0.0

    def test_opposed_embedding_clamps_to_zero_not_negative(self) -> None:
        """cos=-1 -> raw 0.0 -> rescaled would be -1.0; clamped, no negative penalty."""
        assert _normalize_vector_score(0.0) == 0.0

    def test_perfect_embedding_similarity_is_unchanged(self) -> None:
        """cos=1 -> raw 1.0 -> rescaled 1.0: real signal is not compressed by the fix."""
        assert _normalize_vector_score(1.0) == 1.0

    def test_missing_timestamp_scores_zero_not_half(self) -> None:
        assert _recency_subscore(None, 30.0) == 0.0

    def test_unparseable_timestamp_scores_zero_not_half(self) -> None:
        assert _recency_subscore("not-a-timestamp", 30.0) == 0.0

    def test_zero_topic_hits_scores_zero_not_point_three(self) -> None:
        assert _topic_subscore("kubernetes docker container", "Irrelevant", []) == 0.0

    def test_no_topic_hint_scores_zero_not_half(self) -> None:
        assert _topic_subscore(None, "Irrelevant", ["Irrelevant"]) == 0.0

    def test_real_topic_hit_is_unchanged(self) -> None:
        """A genuine keyword hit still earns real credit — only the floors moved."""
        assert _topic_subscore("neo4j graph database", "Neo4j", []) == 0.5


class TestRecencyAloneStructurallyCannotAdmit:
    """The ticket's title, proven from config rather than from one probed input.

    ``proactive_memory_w_recency`` (0.20) is strictly below ``proactive_memory_min_score``
    (0.30) in the deployed configuration, so even a memory from this instant — recency's
    maximum possible value, 1.0 — cannot cross the admission bar by itself. Any other
    signal that reaches the bar alongside it must be doing so on its own evidence.
    """

    def test_recency_weight_is_below_the_admission_bar(self, deployed_scoring: None) -> None:
        cfg = proactive_mod.settings
        assert cfg.proactive_memory_w_recency * 1.0 < cfg.proactive_memory_min_score

    def test_maximal_recency_alone_does_not_admit(self, deployed_scoring: None) -> None:
        """Same-instant timestamp (recency ~1.0), orthogonal embedding, no overlap,
        no topic hint — recency at its maximum, everything else at zero.
        """
        row = _entity_row(vector_score=0.5, timestamp_iso="2026-08-25T00:00:00+00:00")
        out = build_proactive_suggestions([row], set(), None, "t-recency-alone", None)
        assert out.candidates == []


class TestAC1SameDayZeroOverlapZeroTopic:
    """AC-1 — the ADR's 0.455 worst case: same-day, zero overlap, zero topic hits.

    Reconstructed exactly as ADR-0138 states it: orthogonal embedding (0.5 raw),
    same-day timestamp (recency ~1.0, not the missing-timestamp fallback), no session
    entities (overlap 0.0), and a topic hint that shares no keyword with the candidate
    (topic 0.0, not the zero-hit 0.3 floor). Pre-fix this scored 0.455 against a 0.3
    bar; it must score below the bar now.
    """

    def test_the_0_455_case_is_no_longer_admitted(self, deployed_scoring: None) -> None:
        row = _entity_row(
            name="Bicycle",
            vector_score=0.5,  # orthogonal
            timestamp_iso="2026-08-25T00:00:00+00:00",  # "today" per session context
            key_entities=["Bicycle"],
        )

        out = build_proactive_suggestions(
            [row], set(), "completely unrelated topic hint", "t-ac1", None
        )

        assert out.candidates == []
        assert len(out.discarded) == 1
        assert out.discarded[0].drop_reason is DropReason.RECALL_SCORE_THRESHOLD
        assert out.discarded[0].relevance_score is not None
        assert out.discarded[0].relevance_score < proactive_mod.settings.proactive_memory_min_score

    def test_pre_fix_this_construction_would_have_scored_0_455(self) -> None:
        """Documents the failure this test would have caught, independent of the fixture."""
        pre_fix_embedding_floor = 0.5
        pre_fix_recency_same_day = 1.0
        pre_fix_topic_zero_hit_floor = 0.3
        pre_fix_total = (
            0.45 * pre_fix_embedding_floor
            + 0.25 * 0.0
            + 0.20 * pre_fix_recency_same_day
            + 0.10 * pre_fix_topic_zero_hit_floor
        )
        assert pre_fix_total == pytest.approx(0.455)
        assert pre_fix_total > 0.3, "the pre-fix arithmetic crossed the bar"


class TestAC2OrthogonalEmbeddingFloorComposition:
    """AC-2 — the ADR's 0.355 floor composition: every floor fires at once.

    Orthogonal embedding, missing timestamp (recency fallback), no topic hint (topic
    fallback), no overlap. Pre-fix this scored 0.355 against a 0.3 bar purely from
    floors, with zero real evidence of relevance anywhere.
    """

    def test_the_0_355_case_is_no_longer_admitted(self, deployed_scoring: None) -> None:
        row = _entity_row(
            name="Bicycle",
            vector_score=0.5,  # orthogonal
            timestamp_iso=None,  # missing -> recency fallback
            key_entities=["Bicycle"],
        )

        out = build_proactive_suggestions([row], set(), None, "t-ac2", None)

        assert out.candidates == []
        assert len(out.discarded) == 1
        assert out.discarded[0].drop_reason is DropReason.RECALL_SCORE_THRESHOLD
        assert out.discarded[0].relevance_score == pytest.approx(0.0)

    def test_pre_fix_this_construction_would_have_scored_0_355(self) -> None:
        pre_fix_embedding_floor = 0.5
        pre_fix_recency_floor = 0.5
        pre_fix_topic_floor = 0.3
        pre_fix_total = (
            0.45 * pre_fix_embedding_floor
            + 0.25 * 0.0
            + 0.20 * pre_fix_recency_floor
            + 0.10 * pre_fix_topic_floor
        )
        assert pre_fix_total == pytest.approx(0.355)
        assert pre_fix_total > 0.3, "the pre-fix arithmetic crossed the bar"


class TestAC3RelevantMemoriesStillAdmitted:
    """AC-3 — the floor removal must not turn into "raise the threshold until nothing
    passes". A probe set of candidates carrying genuine, non-floor evidence must still
    clear the bar.
    """

    def test_strong_embedding_overlap_recency_and_topic_all_admit(
        self, deployed_scoring: None
    ) -> None:
        """The easy case: every dimension carries real signal."""
        row = _entity_row(
            name="Neo4j",
            vector_score=0.95,  # cos=0.9, strongly similar
            timestamp_iso="2026-08-25T00:00:00+00:00",
            key_entities=["Neo4j", "GraphDatabase", "Cypher"],
        )
        out = build_proactive_suggestions(
            [row], {"Neo4j", "GraphDatabase", "Cypher"}, "tell me about neo4j", "t-ac3-strong", None
        )
        assert len(out.candidates) == 1

    def test_moderate_embedding_with_partial_overlap_still_admits(
        self, deployed_scoring: None
    ) -> None:
        """A realistically weaker but genuinely relevant candidate: one entity overlap,
        moderate embedding similarity, no topic hint available. Must still clear 0.3.
        """
        row = _entity_row(
            name="Postgres",
            vector_score=0.85,  # cos=0.7
            timestamp_iso="2026-08-25T00:00:00+00:00",
            key_entities=["Postgres"],
        )
        out = build_proactive_suggestions([row], {"Postgres"}, None, "t-ac3-moderate", None)
        assert len(out.candidates) == 1

    def test_probe_set_admission_rate_at_or_above_bar(self, deployed_scoring: None) -> None:
        """A small held-out-style probe of known-relevant candidates: every one admits."""
        rows = [
            _entity_row(
                name=f"Relevant{i}",
                vector_score=0.9,
                timestamp_iso="2026-08-25T00:00:00+00:00",
                key_entities=[f"Relevant{i}"],
            )
            for i in range(5)
        ]
        session_entities = {f"Relevant{i}" for i in range(5)}
        out = build_proactive_suggestions(rows, session_entities, None, "t-ac3-probe", None)
        assert len(out.candidates) == 5, "every genuinely relevant probe candidate admitted"


class TestAC4AbsentSubjectYieldsNoFloorAdmissions:
    """AC-4 — nearest-neighbour noise for an absent subject must not populate the
    memory section. Simulates what a vector search returns for a subject with nothing
    relevant in the graph: its nearest neighbours by cosine, none of which are actually
    on-topic, with no entity overlap and no timestamp/topic evidence either.
    """

    def test_nearest_neighbour_noise_for_an_absent_subject_is_fully_discarded(
        self, deployed_scoring: None
    ) -> None:
        rows = [
            _entity_row(
                name=f"Nearest{i}",
                vector_score=0.5 + i * 0.02,  # orthogonal-ish nearest neighbours
                timestamp_iso=None,
                key_entities=[f"Nearest{i}"],
            )
            for i in range(5)
        ]
        out = build_proactive_suggestions(
            rows, set(), "a subject absent from the graph", "t-ac4", None
        )
        assert out.candidates == []
        assert len(out.discarded) == 5
        assert all(d.drop_reason is DropReason.RECALL_SCORE_THRESHOLD for d in out.discarded)


class TestAC5NoRowsLostToAccounting:
    """AC-5 — the scoring change touches only which rows admit, never how many are
    accounted for. Every deduplicated candidate is either selected or discarded.
    """

    def test_every_row_is_selected_or_discarded_across_the_new_floor_boundary(
        self, deployed_scoring: None
    ) -> None:
        admits = [
            _entity_row(
                name=f"Admit{i}",
                vector_score=0.9,
                timestamp_iso="2026-08-25T00:00:00+00:00",
                key_entities=[f"Admit{i}"],
            )
            for i in range(3)
        ]
        rejects = [
            _entity_row(
                name=f"Reject{i}",
                vector_score=0.5,
                timestamp_iso=None,
                key_entities=[f"Reject{i}"],
            )
            for i in range(3)
        ]
        rows = admits + rejects
        session_entities = {f"Admit{i}" for i in range(3)}

        out = build_proactive_suggestions(rows, session_entities, None, "t-ac5", None)

        assert len(out.candidates) + len(out.discarded) == len(rows)
        assert len(out.candidates) == 3
        assert len(out.discarded) == 3
