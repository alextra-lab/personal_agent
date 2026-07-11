"""Tests for the ADR-0114 D5 offline consolidator v0 (FRE-842).

Unit-level: mocked Neo4j driver/session (+ a mocked embedder), no real infra
(see ``test_consolidator_integration.py`` for the live-GDS smoke test).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from scripts.study.consolidator import (
    CandidatePair,
    CategoryMembers,
    TypedDecision,
    _combined_score,
    _cosine,
    _jaccard,
    _ordered_pair,
    apply_canonicalization_to_graph,
    canonicalize,
    decay_and_prune,
    decide_candidate_type,
    embed_category_names,
    generate_candidates_gds,
    generate_candidates_pairwise,
)

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[dict[str, Any]]:
        for record in self._records:
            yield record

    async def single(self) -> dict[str, Any] | None:
        return self._records[0] if self._records else None


class _ScriptedSession:
    """Dispatches canned results by query-substring match; records every call."""

    def __init__(self, responses: list[tuple[str, list[dict[str, Any]]]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.raise_on: str | None = None

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        parameters = parameters or {}
        self.calls.append((query, parameters))
        if self.raise_on and self.raise_on in query:
            raise RuntimeError(f"scripted failure on {self.raise_on}")
        for marker, records in self._responses:
            if marker in query:
                return _FakeResult(records)
        return _FakeResult([])

    async def __aenter__(self) -> "_ScriptedSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeDriver:
    def __init__(self, session: _ScriptedSession) -> None:
        self._session = session

    def session(self) -> _ScriptedSession:
        return self._session


# ---------------------------------------------------------------------------
# _jaccard / _cosine / _combined_score / _ordered_pair
# ---------------------------------------------------------------------------


def test_jaccard_shared_members() -> None:
    assert _jaccard(frozenset({"a", "b"}), frozenset({"b", "c"})) == pytest.approx(1 / 3)


def test_jaccard_no_overlap_is_zero() -> None:
    assert _jaccard(frozenset({"a"}), frozenset({"b"})) == 0.0


def test_jaccard_both_empty_is_zero() -> None:
    assert _jaccard(frozenset(), frozenset()) == 0.0


def test_cosine_identical_vectors_is_one() -> None:
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_is_zero() -> None:
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_vector_is_zero_not_nan() -> None:
    assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_combined_score_falls_back_to_jaccard_without_cosine() -> None:
    assert _combined_score(0.4, None, jaccard_weight=0.6) == pytest.approx(0.4)


def test_combined_score_blends_jaccard_and_cosine() -> None:
    assert _combined_score(0.4, 0.8, jaccard_weight=0.6) == pytest.approx(0.6 * 0.4 + 0.4 * 0.8)


def test_ordered_pair_is_lexicographically_stable() -> None:
    assert _ordered_pair("zebra", "apple") == ("apple", "zebra")
    assert _ordered_pair("apple", "zebra") == ("apple", "zebra")


# ---------------------------------------------------------------------------
# embed_category_names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_category_names_keys_by_normalized_name() -> None:
    categories = [
        CategoryMembers("liver health", "liver health", frozenset({"c1"})),
        CategoryMembers("adverse effect", "adverse effect", frozenset({"c2"})),
    ]
    fake_embed = AsyncMock(return_value=[[1.0, 0.0], [0.0, 1.0]])
    with patch("personal_agent.memory.embeddings.generate_embeddings_batch", fake_embed):
        result = await embed_category_names(categories)

    assert result == {"liver health": [1.0, 0.0], "adverse effect": [0.0, 1.0]}
    fake_embed.assert_awaited_once()


# ---------------------------------------------------------------------------
# generate_candidates_pairwise (Stage 1, pure-Python fallback)
# ---------------------------------------------------------------------------


def test_pairwise_generates_candidates_above_min_jaccard() -> None:
    memberships = {
        "liver health": CategoryMembers("liver health", "liver health", frozenset({"c1", "c2"})),
        "liver function": CategoryMembers(
            "liver function", "liver function", frozenset({"c1", "c2", "c3"})
        ),
        "unrelated topic": CategoryMembers("unrelated topic", "unrelated topic", frozenset({"c9"})),
    }

    candidates = generate_candidates_pairwise(memberships, top_k=10, min_jaccard=0.05)

    pairs = {(c.category_a, c.category_b) for c in candidates}
    # "liver function" < "liver health" lexicographically ('f' < 'h') -> category_a
    assert ("liver health", "liver function") not in pairs  # must be ordered a<=b
    assert ("liver function", "liver health") in pairs
    assert not any(
        "unrelated topic" in (c.category_a, c.category_b) for c in candidates
    )  # zero overlap, filtered
    match = next(c for c in candidates if c.category_a == "liver function")
    assert match.jaccard == pytest.approx(2 / 3)
    assert match.name_cosine is None
    assert match.combined_score == pytest.approx(match.jaccard)


def test_pairwise_respects_top_k() -> None:
    memberships = {
        f"cat{i}": CategoryMembers(f"cat{i}", f"cat{i}", frozenset({"shared", f"c{i}"}))
        for i in range(5)
    }

    candidates = generate_candidates_pairwise(memberships, top_k=2, min_jaccard=0.0)

    assert len(candidates) == 2


def test_pairwise_blends_name_cosine_when_embeddings_supplied() -> None:
    memberships = {
        "a": CategoryMembers("a", "a", frozenset({"c1", "c2"})),
        "b": CategoryMembers("b", "b", frozenset({"c1", "c3"})),
    }
    embeddings = {"a": [1.0, 0.0], "b": [0.0, 1.0]}  # orthogonal -> cosine 0

    candidates = generate_candidates_pairwise(
        memberships, top_k=10, min_jaccard=0.0, name_embeddings=embeddings, jaccard_weight=0.6
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.name_cosine == pytest.approx(0.0)
    assert candidate.combined_score == pytest.approx(0.6 * candidate.jaccard)


# ---------------------------------------------------------------------------
# generate_candidates_gds (Stage 1, GDS-backed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gds_candidates_project_stream_drop_in_order() -> None:
    session = _ScriptedSession(
        [
            (
                "gds.nodeSimilarity.stream",
                [{"a": "liver health", "b": "liver function", "similarity": 0.5}],
            ),
        ]
    )
    driver = _FakeDriver(session)

    candidates = await generate_candidates_gds(driver, top_k=5, similarity_cutoff=0.1)

    queries = [q for q, _ in session.calls]
    assert any("gds.graph.project" in q for q in queries)
    assert any("gds.nodeSimilarity.stream" in q for q in queries)
    assert any("gds.graph.drop" in q for q in queries)
    assert queries.index([q for q in queries if "gds.graph.project" in q][0]) < queries.index(
        [q for q in queries if "gds.nodeSimilarity.stream" in q][0]
    )
    assert queries[-1] == [q for q in queries if "gds.graph.drop" in q][0]
    assert len(candidates) == 1
    assert candidates[0].category_a == "liver function"
    assert candidates[0].category_b == "liver health"
    assert candidates[0].jaccard == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_gds_candidates_drops_projection_even_on_stream_failure() -> None:
    session = _ScriptedSession([])
    session.raise_on = "gds.nodeSimilarity.stream"
    driver = _FakeDriver(session)

    with pytest.raises(RuntimeError):
        await generate_candidates_gds(driver, top_k=5, similarity_cutoff=0.1)

    queries = [q for q, _ in session.calls]
    assert any("gds.graph.drop" in q for q in queries)


@pytest.mark.asyncio
async def test_gds_candidates_excludes_self_pairs() -> None:
    session = _ScriptedSession(
        [
            (
                "gds.nodeSimilarity.stream",
                [{"a": "x", "b": "x", "similarity": 1.0}],
            ),
        ]
    )
    driver = _FakeDriver(session)

    candidates = await generate_candidates_gds(driver, top_k=5, similarity_cutoff=0.1)

    assert candidates == []


@pytest.mark.asyncio
async def test_gds_candidates_dedups_reverse_pairs_keeping_max_similarity() -> None:
    session = _ScriptedSession(
        [
            (
                "gds.nodeSimilarity.stream",
                [
                    {"a": "x", "b": "y", "similarity": 0.4},
                    {"a": "y", "b": "x", "similarity": 0.7},
                ],
            ),
        ]
    )
    driver = _FakeDriver(session)

    candidates = await generate_candidates_gds(driver, top_k=5, similarity_cutoff=0.1)

    assert len(candidates) == 1
    assert candidates[0].jaccard == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_gds_candidates_blends_name_cosine() -> None:
    session = _ScriptedSession(
        [("gds.nodeSimilarity.stream", [{"a": "a", "b": "b", "similarity": 0.5}])]
    )
    driver = _FakeDriver(session)
    embeddings = {"a": [1.0, 0.0], "b": [1.0, 0.0]}  # identical -> cosine 1

    candidates = await generate_candidates_gds(
        driver, top_k=5, similarity_cutoff=0.1, name_embeddings=embeddings, jaccard_weight=0.6
    )

    assert candidates[0].combined_score == pytest.approx(0.6 * 0.5 + 0.4 * 1.0)


# ---------------------------------------------------------------------------
# decide_candidate_type (Stage 2)
# ---------------------------------------------------------------------------


def _pair(
    jaccard: float, cosine: float | None = None, *, jaccard_weight: float = 0.6
) -> CandidatePair:
    return CandidatePair(
        category_a="a",
        category_b="b",
        jaccard=jaccard,
        name_cosine=cosine,
        combined_score=_combined_score(jaccard, cosine, jaccard_weight=jaccard_weight),
    )


def test_high_similarity_above_tau_merge_is_alias() -> None:
    memberships = {
        "a": CategoryMembers("a", "a", frozenset({"c1", "c2", "c3"})),
        "b": CategoryMembers("b", "b", frozenset({"c1", "c2", "c4"})),
    }
    pair = _pair(0.5)  # jaccard 0.5 >= tau 0.4

    decision = decide_candidate_type(pair, memberships, tau_merge=0.4)

    assert decision.decision is TypedDecision.ALIAS


def test_broad_parent_narrow_child_is_subsumed_not_alias_even_above_tau_merge() -> None:
    """ADR-0114 D5's named correctness guard: merging a broader parent into a
    narrower one is an error, not a tuning artefact — the containment check
    must win even when the symmetric jaccard/combined score clears tau_merge.
    """
    memberships = {
        "narrow": CategoryMembers("narrow", "narrow", frozenset({"c1", "c2"})),
        "broad": CategoryMembers("broad", "broad", frozenset({"c1", "c2", "c3", "c4", "c5", "c6"})),
    }
    # jaccard = 2/6 = 0.333, containment(narrow in broad) = 2/2 = 1.0
    pair = CandidatePair(
        category_a="broad",
        category_b="narrow",
        jaccard=1 / 3,
        name_cosine=None,
        combined_score=1 / 3,
    )

    decision = decide_candidate_type(pair, memberships, tau_merge=0.3)

    assert decision.decision is TypedDecision.SUBSUMED_BY


def test_singleton_containment_does_not_force_subsumption() -> None:
    """A 1-member category fully contained in a much larger one is noise, not
    a real hierarchy signal — the min-size guard must let it fall through to
    the normal alias/related/distinct ladder instead of forcing SUBSUMED_BY.
    """
    memberships = {
        "tiny": CategoryMembers("tiny", "tiny", frozenset({"c1"})),
        "broad": CategoryMembers("broad", "broad", frozenset({"c1", "c2", "c3", "c4", "c5", "c6"})),
    }
    pair = CandidatePair(
        category_a="broad", category_b="tiny", jaccard=1 / 6, name_cosine=None, combined_score=1 / 6
    )

    decision = decide_candidate_type(pair, memberships, tau_merge=0.9)

    assert decision.decision is not TypedDecision.SUBSUMED_BY


def test_low_similarity_is_distinct() -> None:
    memberships = {
        "a": CategoryMembers("a", "a", frozenset({"c1", "c2", "c3", "c4"})),
        "b": CategoryMembers("b", "b", frozenset({"c1", "c5", "c6", "c7"})),
    }
    pair = _pair(0.05)

    decision = decide_candidate_type(pair, memberships, tau_merge=0.4, related_floor=0.2)

    assert decision.decision is TypedDecision.DISTINCT


def test_mid_similarity_below_related_floor_gap_is_related() -> None:
    memberships = {
        "a": CategoryMembers("a", "a", frozenset({"c1", "c2", "c3", "c4"})),
        "b": CategoryMembers("b", "b", frozenset({"c1", "c2", "c5", "c6"})),
    }
    pair = _pair(0.25)

    decision = decide_candidate_type(
        pair, memberships, tau_merge=0.4, related_floor=0.2, uncertain_margin=0.0
    )

    assert decision.decision is TypedDecision.RELATED


def test_just_below_tau_merge_within_margin_is_uncertain() -> None:
    memberships = {
        "a": CategoryMembers("a", "a", frozenset({"c1", "c2", "c3", "c4"})),
        "b": CategoryMembers("b", "b", frozenset({"c1", "c2", "c3", "c5"})),
    }
    pair = _pair(0.35)

    decision = decide_candidate_type(pair, memberships, tau_merge=0.4, uncertain_margin=0.1)

    assert decision.decision is TypedDecision.UNCERTAIN


# ---------------------------------------------------------------------------
# canonicalize (union-find + deterministic representative selection)
# ---------------------------------------------------------------------------


def test_canonicalize_transitive_merge_groups_all_three() -> None:
    memberships = {
        "a": CategoryMembers("a", "a", frozenset({"c1"})),
        "b": CategoryMembers("b", "b", frozenset({"c1"})),
        "c": CategoryMembers("c", "c", frozenset({"c1"})),
    }
    # a-b alias, b-c alias, a-c never directly compared — must still land in one group
    candidates = [
        CandidatePair("a", "b", 0.9, None, 0.9),
        CandidatePair("b", "c", 0.9, None, 0.9),
    ]

    result = canonicalize(memberships, candidates, tau_merge=0.5)

    assert result.canonical_of["a"] == result.canonical_of["b"] == result.canonical_of["c"]
    assert result.canonical_category_count == 1


def test_canonicalize_only_merges_alias_decisions() -> None:
    memberships = {
        "narrow": CategoryMembers("narrow", "narrow", frozenset({"c1", "c2"})),
        "broad": CategoryMembers("broad", "broad", frozenset({"c1", "c2", "c3", "c4", "c5", "c6"})),
        "distinct": CategoryMembers("distinct", "distinct", frozenset({"c9"})),
    }
    candidates = [
        CandidatePair("broad", "narrow", 1 / 3, None, 1 / 3),  # -> SUBSUMED_BY, no merge
    ]

    result = canonicalize(memberships, candidates, tau_merge=0.3)

    assert result.canonical_of["narrow"] == "narrow"
    assert result.canonical_of["broad"] == "broad"
    assert result.canonical_category_count == 3


def test_canonicalize_representative_is_largest_member_set_deterministic_tiebreak() -> None:
    """Sized so the pair clears tau_merge as a genuine alias WITHOUT tripping
    the containment/size-ratio subsumption guard (containment 2/3 < 0.8,
    size_ratio 4/3 < 2.0) — isolates the representative-selection tiebreak
    from the (separately tested) subsumption guard.
    """
    memberships = {
        "small": CategoryMembers("small", "small", frozenset({"c1", "c2", "c4"})),
        "big": CategoryMembers("big", "big", frozenset({"c1", "c2", "c3", "c5"})),
    }
    jaccard = 2 / 5  # shared={c1,c2}=2, union=5
    candidates = [CandidatePair("big", "small", jaccard, None, jaccard)]

    result = canonicalize(memberships, candidates, tau_merge=0.35)

    assert result.canonical_of["small"] == "big"
    assert result.canonical_of["big"] == "big"


def test_canonicalize_representative_selection_is_order_independent() -> None:
    memberships = {
        "a": CategoryMembers("a", "a", frozenset({"c1"})),
        "b": CategoryMembers("b", "b", frozenset({"c1", "c2"})),
        "c": CategoryMembers("c", "c", frozenset({"c1", "c2", "c3"})),
    }
    forward = [
        CandidatePair("a", "b", 0.9, None, 0.9),
        CandidatePair("b", "c", 0.9, None, 0.9),
    ]
    reversed_order = list(reversed(forward))

    result_forward = canonicalize(memberships, forward, tau_merge=0.5)
    result_reversed = canonicalize(memberships, reversed_order, tau_merge=0.5)

    assert (
        result_forward.canonical_of
        == result_reversed.canonical_of
        == {
            "a": "c",
            "b": "c",
            "c": "c",
        }
    )


# ---------------------------------------------------------------------------
# apply_canonicalization_to_graph
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_canonicalization_writes_canonicalized_as_edges() -> None:
    session = _ScriptedSession([("MATCH (a:MembershipAssertion)", [])])
    memberships = {
        "small": CategoryMembers("small", "small", frozenset({"c1"})),
        "big": CategoryMembers("big", "big", frozenset({"c1", "c2"})),
    }
    result = canonicalize(
        memberships, [CandidatePair("big", "small", 0.5, None, 0.5)], tau_merge=0.4
    )

    await apply_canonicalization_to_graph(session, result, tau_merge=0.4)

    queries = [q for q, _ in session.calls]
    assert any("CANONICALIZED_AS" in q for q in queries)


@pytest.mark.asyncio
async def test_apply_canonicalization_never_touches_membership_assertion_writes() -> None:
    """Evidence is immutable (D2) — the write-back may only create/delete
    `MEMBER_OF` (derived) and `CANONICALIZED_AS` (derived) edges, never a
    `SET`/`CREATE`/`DELETE` against a `MembershipAssertion` node.
    """
    session = _ScriptedSession([])
    memberships = {
        "small": CategoryMembers("small", "small", frozenset({"c1"})),
        "big": CategoryMembers("big", "big", frozenset({"c1", "c2"})),
    }
    result = canonicalize(
        memberships, [CandidatePair("big", "small", 0.5, None, 0.5)], tau_merge=0.4
    )

    await apply_canonicalization_to_graph(session, result, tau_merge=0.4)

    for query, _ in session.calls:
        if "MembershipAssertion" in query:
            assert "MATCH" in query and "CREATE (a:MembershipAssertion" not in query
            assert "SET a." not in query
            assert "DELETE a" not in query


@pytest.mark.asyncio
async def test_apply_canonicalization_deletes_superseded_member_of_edges() -> None:
    session = _ScriptedSession([])
    memberships = {
        "small": CategoryMembers("small", "small", frozenset({"c1"})),
        "big": CategoryMembers("big", "big", frozenset({"c1", "c2"})),
    }
    result = canonicalize(
        memberships, [CandidatePair("big", "small", 0.5, None, 0.5)], tau_merge=0.4
    )

    await apply_canonicalization_to_graph(session, result, tau_merge=0.4)

    queries = [q for q, _ in session.calls]
    assert any("DELETE" in q and "MEMBER_OF" in q or ("DELETE m" in q) for q in queries)


# ---------------------------------------------------------------------------
# decay_and_prune
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decay_and_prune_dry_run_does_not_mutate() -> None:
    session = _ScriptedSession(
        [
            (
                "MATCH (c:Concept)-[m:MEMBER_OF]->(cat:Category)",
                [
                    {
                        "concept_id": "c1",
                        "category_normalized_name": "x",
                        "membership_confidence": 0.5,
                        "last_supported_at": (
                            datetime.now(timezone.utc) - timedelta(days=100)
                        ).isoformat(),
                    }
                ],
            )
        ]
    )

    report = await decay_and_prune(
        session,
        reference_time=datetime.now(timezone.utc),
        decay_factor=0.5,
        floor=0.3,
        stale_after=timedelta(days=30),
        apply=False,
    )

    assert report.would_suppress_count >= 0
    queries = [q for q, _ in session.calls]
    assert not any("SET m.membership_confidence" in q for q in queries)
    assert not any(("DELETE m" in q) for q in queries)


@pytest.mark.asyncio
async def test_decay_and_prune_apply_only_touches_member_of() -> None:
    session = _ScriptedSession(
        [
            (
                "MATCH (c:Concept)-[m:MEMBER_OF]->(cat:Category)",
                [
                    {
                        "concept_id": "c1",
                        "category_normalized_name": "x",
                        "membership_confidence": 0.31,
                        "last_supported_at": (
                            datetime.now(timezone.utc) - timedelta(days=100)
                        ).isoformat(),
                    }
                ],
            )
        ]
    )

    await decay_and_prune(
        session,
        reference_time=datetime.now(timezone.utc),
        decay_factor=0.5,
        floor=0.3,
        stale_after=timedelta(days=30),
        apply=True,
    )

    for query, _ in session.calls:
        assert "MembershipAssertion" not in query
