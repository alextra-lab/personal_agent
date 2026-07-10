"""Tests for the ADR-0114 AC-4 scoring rig (FRE-840).

Unit-only, no substrate: pure statistical functions over synthetic paired
scores. A fixed ``random.Random`` seed makes the bootstrap resampling tests
deterministic.
"""

from __future__ import annotations

import random

import pytest
from scripts.study.scoring_rig import (
    CueScore,
    evaluate_ac4,
    non_inferiority_test,
    paired_bootstrap_ci,
    paired_ndcg_non_inferiority,
    paired_recall_comparison,
    paired_significance,
    score_cue,
)

# ---------------------------------------------------------------------------
# score_cue — thin wrapper over the reused recall_at_k/ndcg_at_k
# ---------------------------------------------------------------------------


def test_score_cue_computes_recall_and_ndcg() -> None:
    score = score_cue("cue-1", ["entity:a", "entity:b", "entity:c"], {"entity:a", "entity:c"}, k=20)
    assert score.cue_id == "cue-1"
    assert score.recall_at_k == 1.0
    assert score.ndcg_at_k is not None


def test_score_cue_none_when_gold_empty() -> None:
    score = score_cue("cue-1", ["entity:a"], set(), k=20)
    assert score.recall_at_k is None
    assert score.ndcg_at_k is None


def test_score_cue_rejects_non_positive_k() -> None:
    with pytest.raises(ValueError):
        score_cue("cue-1", ["entity:a"], {"entity:a"}, k=0)


# ---------------------------------------------------------------------------
# paired_bootstrap_ci — the effect size + 95% CI primitive
# ---------------------------------------------------------------------------


def test_paired_bootstrap_ci_all_positive_diffs_excludes_zero() -> None:
    diffs = [0.3, 0.25, 0.4, 0.35, 0.28, 0.31, 0.29, 0.33]
    result = paired_bootstrap_ci(diffs, n_resamples=5000, rng=random.Random(42))
    assert result.point_estimate == pytest.approx(sum(diffs) / len(diffs))
    assert result.ci_low > 0.0
    assert result.ci_low < result.point_estimate < result.ci_high


def test_paired_bootstrap_ci_symmetric_diffs_includes_zero() -> None:
    diffs = [0.1, -0.1, 0.05, -0.05, 0.02, -0.02, 0.0, 0.01, -0.01]
    result = paired_bootstrap_ci(diffs, n_resamples=5000, rng=random.Random(7))
    assert result.ci_low < 0.0 < result.ci_high


def test_paired_bootstrap_ci_empty_raises() -> None:
    with pytest.raises(ValueError):
        paired_bootstrap_ci([], rng=random.Random(1))


def test_paired_bootstrap_ci_deterministic_with_same_seed() -> None:
    diffs = [0.2, 0.1, 0.3, -0.05, 0.15]
    a = paired_bootstrap_ci(diffs, n_resamples=2000, rng=random.Random(99))
    b = paired_bootstrap_ci(diffs, n_resamples=2000, rng=random.Random(99))
    assert a == b


# ---------------------------------------------------------------------------
# paired_significance / non_inferiority_test
# ---------------------------------------------------------------------------


def test_paired_significance_positive_diffs_is_significant() -> None:
    diffs = [0.3, 0.25, 0.4, 0.35, 0.28, 0.31, 0.29, 0.33]
    result = paired_significance(diffs, n_resamples=5000, rng=random.Random(42))
    assert result.significant is True


def test_paired_significance_symmetric_diffs_not_significant() -> None:
    diffs = [0.1, -0.1, 0.05, -0.05, 0.02, -0.02, 0.0, 0.01, -0.01]
    result = paired_significance(diffs, n_resamples=5000, rng=random.Random(7))
    assert result.significant is False


def test_non_inferiority_holds_with_generous_margin() -> None:
    diffs = [-0.02, -0.01, 0.0, -0.015, 0.01, -0.03, 0.0]
    result = non_inferiority_test(diffs, margin=0.20, n_resamples=5000, rng=random.Random(3))
    assert result.holds is True


def test_non_inferiority_fails_with_tight_margin() -> None:
    diffs = [-0.5, -0.4, -0.6, -0.45, -0.55]
    result = non_inferiority_test(diffs, margin=0.01, n_resamples=5000, rng=random.Random(3))
    assert result.holds is False


# ---------------------------------------------------------------------------
# _paired_diffs (exercised via the public paired_* comparison functions) —
# id/order validation and None-exclusion (codex review finding)
# ---------------------------------------------------------------------------


def test_paired_recall_comparison_mismatched_ids_raises() -> None:
    baseline = [CueScore("a", 0.5, 0.5)]
    study = [CueScore("b", 0.6, 0.6)]
    with pytest.raises(ValueError):
        paired_recall_comparison(baseline, study, rng=random.Random(1))


def test_paired_recall_comparison_mismatched_length_raises() -> None:
    baseline = [CueScore("a", 0.5, 0.5), CueScore("b", 0.5, 0.5)]
    study = [CueScore("a", 0.6, 0.6)]
    with pytest.raises(ValueError):
        paired_recall_comparison(baseline, study, rng=random.Random(1))


def test_paired_recall_comparison_excludes_none_pairs_and_reports_count() -> None:
    baseline = [
        CueScore("a", 0.5, 0.5),
        CueScore("b", None, None),  # empty gold set for this cue
        CueScore("c", 0.4, 0.4),
    ]
    study = [
        CueScore("a", 0.8, 0.8),
        CueScore("b", None, None),
        CueScore("c", 0.7, 0.7),
    ]
    result = paired_recall_comparison(baseline, study, n_resamples=2000, rng=random.Random(5))
    assert result.excluded_count == 1
    assert result.ci.point_estimate == pytest.approx(0.3, abs=1e-9)


def test_paired_ndcg_non_inferiority_excludes_none_pairs() -> None:
    baseline = [CueScore("a", 0.5, 0.6), CueScore("b", 0.4, None)]
    study = [CueScore("a", 0.6, 0.65), CueScore("b", 0.5, None)]
    result = paired_ndcg_non_inferiority(
        baseline, study, margin=0.1, n_resamples=2000, rng=random.Random(2)
    )
    assert result.excluded_count == 1


# ---------------------------------------------------------------------------
# evaluate_ac4 — the full AC-4(i-iii) + non-inferiority combinator
# ---------------------------------------------------------------------------


def _cues(recalls: list[float], ndcgs: list[float]) -> list[CueScore]:
    return [CueScore(f"cue-{i}", r, n) for i, (r, n) in enumerate(zip(recalls, ndcgs, strict=True))]


def test_evaluate_ac4_passes_when_all_bars_clear() -> None:
    baseline = _cues([0.4] * 10, [0.7] * 10)
    study = _cues([0.55] * 10, [0.72] * 10)  # 1.375x relative, +0.15 absolute
    verdict = evaluate_ac4(
        baseline,
        study,
        relative_bar=1.10,
        absolute_floor=0.05,
        ndcg_margin=0.05,
        n_resamples=2000,
        rng=random.Random(11),
    )
    assert verdict.relative_lift == pytest.approx(1.375)
    assert verdict.absolute_lift == pytest.approx(0.15)
    assert verdict.passes is True


def test_evaluate_ac4_fails_when_relative_bar_missed() -> None:
    baseline = _cues([0.4] * 10, [0.7] * 10)
    study = _cues([0.42] * 10, [0.72] * 10)  # only 1.05x — misses the 1.10x bar
    verdict = evaluate_ac4(
        baseline,
        study,
        relative_bar=1.10,
        absolute_floor=0.01,
        ndcg_margin=0.05,
        n_resamples=2000,
        rng=random.Random(11),
    )
    assert verdict.passes is False


def test_evaluate_ac4_fails_when_ndcg_non_inferiority_breached() -> None:
    baseline = _cues([0.4] * 10, [0.8] * 10)
    study = _cues([0.6] * 10, [0.3] * 10)  # recall wins big, but nDCG collapses
    verdict = evaluate_ac4(
        baseline,
        study,
        relative_bar=1.10,
        absolute_floor=0.05,
        ndcg_margin=0.05,
        n_resamples=2000,
        rng=random.Random(11),
    )
    assert verdict.ndcg_non_inferiority.holds is False
    assert verdict.passes is False


def test_evaluate_ac4_never_vacuously_passes_on_empty_input() -> None:
    with pytest.raises(ValueError):
        evaluate_ac4(
            [],
            [],
            relative_bar=1.10,
            absolute_floor=0.05,
            ndcg_margin=0.05,
            rng=random.Random(11),
        )
