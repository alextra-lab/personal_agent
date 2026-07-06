"""FRE-817 -- unit tests for the pure corpus A/B nDCG scoring core."""

from __future__ import annotations

import pytest
from scripts.eval.fre435_memory_recall.probes import ExpectedRecall, ProbeCase
from scripts.eval.fre817_corpus_ab_embedder.corpus_ab import score_arm

#: A tiny 2D embedding space: "cat" on axis 0, "dog" on axis 1, "car" diagonal.
_NOTE_NAMES = ["cat", "dog", "car"]
_NOTE_VECS = [[1.0, 0.0], [0.0, 1.0], [0.7071067811865476, 0.7071067811865476]]


def _case(case_id: str, query_vec_hint: str, expected: tuple[str, ...]) -> ProbeCase:
    return ProbeCase(
        case_id=case_id,
        query=f"query about {query_vec_hint}",
        expected=ExpectedRecall(entity_names=expected),
    )


def test_perfect_rank1_hits_score_ndcg_1_at_every_k() -> None:
    """Every relevant note ranked first scores nDCG@1 == nDCG@5 == 1.0."""
    cases = [_case("cat-case", "cats", ("cat",)), _case("dog-case", "dogs", ("dog",))]
    query_vecs = [[1.0, 0.0], [0.0, 1.0]]
    result = score_arm(cases, _NOTE_NAMES, _NOTE_VECS, query_vecs, ks=(1, 5))
    assert result[1] == pytest.approx(1.0)
    assert result[5] == pytest.approx(1.0)


def test_rank3_hit_scores_zero_at_k1_and_half_at_k5() -> None:
    """A relevant note ranked 3rd of 3 misses at k=1 but scores 0.5 at k=5."""
    # query is mostly "cat"-aligned but expects "dog" (rank 3 of 3) -- an
    # imperfect-ranking case: ndcg@1 = 0 (miss), ndcg@5 ideal_hits=1 so
    # idcg = 1/log2(2) = 1, dcg = 1/log2(4) = 0.5 -> ndcg@5 = 0.5.
    cases = [_case("mismatch-case", "cats but really means dogs", ("dog",))]
    query_vecs = [[0.9, 0.1]]
    result = score_arm(cases, _NOTE_NAMES, _NOTE_VECS, query_vecs, ks=(1, 5))
    assert result[1] == pytest.approx(0.0)
    assert result[5] == pytest.approx(0.5)


def test_control_case_excluded_from_the_mean() -> None:
    """A control (empty expected) contributes None and must not skew the mean."""
    cases = [
        _case("cat-case", "cats", ("cat",)),
        _case("dog-case", "dogs", ("dog",)),
        _case("mismatch-case", "cats but really means dogs", ("dog",)),
        _case("control-case", "something with no note at all", ()),
    ]
    query_vecs = [[1.0, 0.0], [0.0, 1.0], [0.9, 0.1], [0.0, 0.0]]
    # a zero query vector is degenerate for cosine; give the control a vector
    # that doesn't match anything strongly instead, to keep _unit well-defined.
    query_vecs[3] = [0.1, -0.1]
    result = score_arm(cases, _NOTE_NAMES, _NOTE_VECS, query_vecs, ks=(1, 5))
    assert result[1] == pytest.approx((1.0 + 1.0 + 0.0) / 3)
    assert result[5] == pytest.approx((1.0 + 1.0 + 0.5) / 3)
