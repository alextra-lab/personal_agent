"""FRE-488 — D1 metric core tests (pure; no substrate, no LLM).

Every metric is checked against a hand-computed fixture. The metrics operate on
namespace-agnostic id sequences/sets — namespacing is the harness's job.
"""

from __future__ import annotations

import math

import pytest
from scripts.eval.fre435_memory_recall.metrics import (
    WriteOutcome,
    extraction_fire_rate,
    false_negative,
    k_sweep,
    landing_rate,
    mean_optional,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    retrieval_miss,
)

# retrieved order: c (miss), a (hit), b (miss) ; relevant = {a, d}
RETRIEVED = ["c", "a", "b"]
RELEVANT = {"a", "d"}


# ---------------------------------------------------------------------------
# recall@k / precision@k
# ---------------------------------------------------------------------------


def test_recall_at_k_basic() -> None:
    """Recall at k basic."""
    assert recall_at_k(RETRIEVED, RELEVANT, 1) == 0.0  # only "c" seen, not relevant
    assert recall_at_k(RETRIEVED, RELEVANT, 2) == 0.5  # "a" of {a,d} -> 1/2
    assert recall_at_k(RETRIEVED, RELEVANT, 3) == 0.5  # "d" never retrieved


def test_recall_at_k_empty_relevant_is_none() -> None:
    """Recall at k empty relevant is none."""
    # |relevant| == 0 must be None (excluded from aggregate), never 1.0 (codex Q3).
    assert recall_at_k(["a", "b"], set(), 2) is None


def test_precision_at_k_basic() -> None:
    """Precision at k basic."""
    assert precision_at_k(RETRIEVED, RELEVANT, 1) == 0.0
    assert precision_at_k(RETRIEVED, RELEVANT, 2) == 0.5  # 1 hit in top-2
    assert precision_at_k(RETRIEVED, RELEVANT, 3) == pytest.approx(1 / 3)


def test_k_at_least_one() -> None:
    """K at least one."""
    with pytest.raises(ValueError):
        recall_at_k(RETRIEVED, RELEVANT, 0)


# ---------------------------------------------------------------------------
# reciprocal rank / nDCG
# ---------------------------------------------------------------------------


def test_reciprocal_rank() -> None:
    """Reciprocal rank."""
    assert reciprocal_rank(RETRIEVED, RELEVANT) == 0.5  # first relevant at rank 2
    assert reciprocal_rank(["a"], RELEVANT) == 1.0
    assert reciprocal_rank(["x", "y"], RELEVANT) == 0.0
    assert reciprocal_rank(["x"], set()) is None


def test_ndcg_single_relevant_at_rank2() -> None:
    """Ndcg single relevant at rank2."""
    # retrieved [c, a], relevant {a}: DCG = 1/log2(2+1); IDCG (1 ideal hit) = 1/log2(1+1)=1
    expected = (1 / math.log2(3)) / 1.0
    assert ndcg_at_k(["c", "a"], {"a"}, 2) == pytest.approx(expected)


def test_ndcg_idcg_uses_min_k_relevant() -> None:
    """Ndcg idcg uses min k relevant."""
    # |relevant|=3 but k=2: IDCG must use min(k,|rel|)=2 ideal hits, not 3 (codex Q3).
    # perfect ranking of two relevant in top-2 -> nDCG == 1.0
    assert ndcg_at_k(["a", "b"], {"a", "b", "c"}, 2) == pytest.approx(1.0)


def test_ndcg_empty_relevant_is_none() -> None:
    """Ndcg empty relevant is none."""
    assert ndcg_at_k(["a"], set(), 2) is None


# ---------------------------------------------------------------------------
# false-negative vs retrieval-miss (codex Q3 — distinct)
# ---------------------------------------------------------------------------


def test_false_negative_empty_retrieval() -> None:
    """False negative empty retrieval."""
    assert false_negative([], {"a"}, denied=False) is True


def test_false_negative_denied_despite_results() -> None:
    """False negative denied despite results."""
    assert false_negative(["a"], {"a"}, denied=True) is True


def test_false_negative_false_when_results_and_not_denied() -> None:
    """False negative false when results and not denied."""
    # Returned something and didn't deny -> NOT a false negative, even if wrong.
    assert false_negative(["x"], {"a"}, denied=False) is False


def test_false_negative_none_when_no_relevant() -> None:
    """False negative none when no relevant."""
    assert false_negative([], set(), denied=True) is None


def test_retrieval_miss_nonempty_but_wrong() -> None:
    """Retrieval miss nonempty but wrong."""
    # Returned content, not denied, but the relevant item is absent at k -> a miss
    # that false_negative does NOT catch.
    assert retrieval_miss(["x", "y"], {"a"}, k=2) is True
    assert false_negative(["x", "y"], {"a"}, denied=False) is False


def test_retrieval_miss_false_when_hit() -> None:
    """Retrieval miss false when hit."""
    assert retrieval_miss(["a"], {"a"}, k=1) is False


def test_retrieval_miss_none_when_no_relevant() -> None:
    """Retrieval miss none when no relevant."""
    assert retrieval_miss(["x"], set(), k=1) is None


# ---------------------------------------------------------------------------
# k-sweep
# ---------------------------------------------------------------------------


def test_k_sweep_separates_index_from_rank() -> None:
    """K sweep separates index from rank."""
    sweep = k_sweep(RETRIEVED, RELEVANT, [1, 2, 3])
    assert sweep[1].recall == 0.0
    assert sweep[2].recall == 0.5
    assert sweep[3].recall == 0.5
    assert sweep[2].precision == 0.5


# ---------------------------------------------------------------------------
# write-completeness aggregates
# ---------------------------------------------------------------------------


def test_extraction_fire_and_landing_rates() -> None:
    """Extraction fire and landing rates."""
    outcomes = [
        WriteOutcome(extraction_fired=True, entities_landed=2, entities_expected=2),
        WriteOutcome(extraction_fired=True, entities_landed=0, entities_expected=1),
        WriteOutcome(extraction_fired=False, entities_landed=0, entities_expected=1),
    ]
    assert extraction_fire_rate(outcomes) == pytest.approx(2 / 3)
    # landing = landed / expected, aggregated over expected totals: (2+0+0)/(2+1+1)
    assert landing_rate(outcomes) == pytest.approx(2 / 4)


def test_landing_rate_zero_expected_is_none() -> None:
    """Landing rate zero expected is none."""
    assert (
        landing_rate([WriteOutcome(extraction_fired=True, entities_landed=0, entities_expected=0)])
        is None
    )


# ---------------------------------------------------------------------------
# mean_optional helper (drops None, codex Q3)
# ---------------------------------------------------------------------------


def test_mean_optional_drops_none() -> None:
    """Mean optional drops none."""
    assert mean_optional([1.0, None, 0.0]) == 0.5
    assert mean_optional([None, None]) is None
    assert mean_optional([]) is None
