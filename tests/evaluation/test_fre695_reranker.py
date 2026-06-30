"""FRE-695 — pure unit tests for the reranker-separation arm (no network).

Covers the three pure pieces of the cross-encoder benchmark:
* `parse_rerank_response` — both wire shapes (llama.cpp `results[]`, Voyage `data[]`),
  re-aligned to the input document order, fail-loud on a truncated response;
* `best_separation_at_observed` — best Youden's J swept at the *observed* scores (not a
  fixed grid), so a compressed reranker score band cannot understate separation;
* `separation_from_scores` — the per-expected-entity-positive / top-non-match-negative
  extraction shared with the FRE-694 metric, on a score matrix.
"""

from __future__ import annotations

import pytest
from scripts.eval.fre435_memory_recall.separation_benchmark import (
    parse_rerank_response,
    separation_from_scores,
)
from scripts.eval.fre435_memory_recall.separation_report import best_separation_at_observed


def test_parse_rerank_response_llamacpp_results_shape() -> None:
    """llama.cpp `results[]` are re-aligned to input order by `index`."""
    payload = {
        "results": [
            {"index": 2, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.1},
            {"index": 1, "relevance_score": 0.5},
        ]
    }
    assert parse_rerank_response(payload, 3) == pytest.approx([0.1, 0.5, 0.9])


def test_parse_rerank_response_voyage_data_shape() -> None:
    """Voyage `data[]` is handled (an intentional delta from reranker.py)."""
    payload = {
        "data": [
            {"index": 0, "relevance_score": 0.81},
            {"index": 1, "relevance_score": 0.22},
        ]
    }
    assert parse_rerank_response(payload, 2) == pytest.approx([0.81, 0.22])


def test_parse_rerank_response_fails_loud_on_truncation() -> None:
    """A response with fewer results than documents raises (never score a truncated set)."""
    payload = {"results": [{"index": 0, "relevance_score": 0.9}]}
    with pytest.raises(ValueError, match="truncated|count"):
        parse_rerank_response(payload, 3)


def test_best_separation_at_observed_clean() -> None:
    """A cleanly separated pair yields Youden's J = 1.0 at a separating threshold."""
    fp = best_separation_at_observed(positives=[0.90, 0.95], negatives=[0.10, 0.20])
    assert fp.youden_j == pytest.approx(1.0)
    assert fp.recall == pytest.approx(1.0)
    assert fp.false_positive_rate == pytest.approx(0.0)


def test_best_separation_at_observed_overlapping_beats_fixed_grid() -> None:
    """Overlapping clouds: best J is found at an observed score, not a coarse grid."""
    # pos {0.70,0.80}, neg {0.60,0.75}: best J=0.5 (at t=0.70 keep both pos, 1 neg; or t=0.80).
    fp = best_separation_at_observed(positives=[0.70, 0.80], negatives=[0.60, 0.75])
    assert fp.youden_j == pytest.approx(0.5)


def test_separation_from_scores_per_entity_and_top_nonmatch() -> None:
    """Positives are per-expected-entity; negatives are each query's top non-match."""
    from scripts.eval.fre435_memory_recall.probes import (
        ExpectedRecall,
        ProbeCase,
        SeedEntity,
    )

    note_names = ["alpha", "beta", "gamma"]
    cases = [
        ProbeCase(
            case_id="pos-compound",
            query="q1",
            seed_entities=(SeedEntity(name="Alpha"), SeedEntity(name="Beta")),
            expected=ExpectedRecall(entity_names=("Alpha", "Beta")),
            tags=("type:positive",),
        ),
        ProbeCase(
            case_id="ctrl",
            query="q2",
            expected=ExpectedRecall(entity_names=(), must_not_deny=False),
            tags=("type:control",),
        ),
    ]
    # score_rows[i][j] = score of query i vs note_names[j]
    score_rows = [
        [0.8, 0.6, 0.3],  # pos case: alpha=0.8, beta=0.6 (both expected); gamma=0.3 top non-match
        [0.2, 0.9, 0.4],  # control: top score 0.9 (all non-match)
    ]
    positives, negatives = separation_from_scores(cases, note_names, score_rows)
    assert sorted(positives) == pytest.approx([0.6, 0.8])  # per-entity: alpha + beta
    assert sorted(negatives) == pytest.approx([0.3, 0.9])  # pos top-non-match + control top
