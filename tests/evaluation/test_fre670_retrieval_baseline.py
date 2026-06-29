"""FRE-670 — pure unit tests for the BM25 keyword-baseline arm (keyword_baseline.py).

Covers the two correctness traps codex flagged when reusing the baseline as the
standing lexical-leakage guard (plan-review 2026-06-29):

* a document with BM25 score 0 is NOT a hit (a vocab-divergent query that matches
  nothing must not score a phantom recall on insertion order);
* ties break deterministically by name, so the ranking does not depend on corpus
  insertion order.

The register-split aggregation (natural vs imagery recall) is the FRE-670 reporting
addition; tested here on a synthetic probe so it needs no substrate or embedder.
"""

from __future__ import annotations

from scripts.eval.fre435_memory_recall.keyword_baseline import (
    bm25_rank,
    evaluate_probe,
    fractional_recall_at_k,
)


def test_zero_score_docs_are_not_ranked() -> None:
    """A query that shares no content tokens with a doc never ranks that doc."""
    docs = [["alpha", "beta"], ["gamma", "delta"], ["alpha", "alpha"]]
    names = ["AB", "GD", "AA"]
    ranked = bm25_rank(["alpha"], docs, names)
    # GD shares nothing with the query → excluded entirely (not a phantom tail hit).
    assert "GD" not in ranked
    assert set(ranked) == {"AB", "AA"}


def test_ties_break_deterministically_by_name() -> None:
    """Equal-score docs order by name, independent of insertion order."""
    docs_a = [["x"], ["x"]]
    ranked_a = bm25_rank(["x"], docs_a, ["zebra", "apple"])
    ranked_b = bm25_rank(["x"], docs_a, ["apple", "zebra"])
    assert ranked_a == ["apple", "zebra"]
    assert ranked_b == ["apple", "zebra"]


def test_fractional_recall_at_k() -> None:
    """Recall@k is fractional over the expected set (matches the harness metric)."""
    ranked = ["x", "y", "z"]
    assert fractional_recall_at_k(ranked, {"x", "z"}, 2) == 0.5  # only x in top-2
    assert fractional_recall_at_k(ranked, {"x", "z"}, 3) == 1.0  # both in top-3
    assert fractional_recall_at_k(ranked, set(), 5) == 0.0  # no expected → 0 by convention
    assert fractional_recall_at_k([], {"x"}, 5) == 0.0


def test_evaluate_probe_splits_by_register() -> None:
    """evaluate_probe reports recall per register over positives only."""
    cases = [
        {
            "case_id": "img-hit",
            "tags": ["type:positive", "register:imagery"],
            "seed_entities": [{"name": "Photosynthesis", "description": "plants convert light"}],
            "expected": {"entity_names": ["Photosynthesis"]},
            "query": "how greenery turns sunshine into food",  # divergent → likely a keyword miss
        },
        {
            "case_id": "nat-hit",
            "tags": ["type:positive", "register:natural"],
            "seed_entities": [{"name": "Photosynthesis", "description": "plants convert light"}],
            "expected": {"entity_names": ["Photosynthesis"]},
            "query": "tell me about how plants convert light",  # lexical overlap → keyword hit
        },
        {
            "case_id": "ctrl",
            "tags": ["type:control"],
            "expected": {"entity_names": []},
            "query": "what about quantum gravity",
        },
    ]
    result = evaluate_probe(cases, use_description=True, k_values=(1, 5))
    # Both registers are represented; the natural case (lexical) out-recalls the imagery one.
    assert set(result.recall_by_register) == {"natural", "imagery"}
    assert result.recall_by_register["natural"][5] >= result.recall_by_register["imagery"][5]
    # Controls are not scored as positives.
    assert result.positives_scored == 2
