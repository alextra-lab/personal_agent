"""FRE-770 — pure unit tests for Fleiss' kappa + pairwise agreement (IAA).

Fixture arithmetic is hand-verified in the implementation plan
(docs/superpowers/plans/2026-07-04-fre-770-gold-relabel-iaa.md): 2 categories,
3 raters, 4 items -> P_bar=2/3, P_bar_e=1/2, kappa=1/3.
"""

from __future__ import annotations

import pytest
from scripts.eval.fre630_extraction_quality.iaa import (
    build_iaa_report,
    fleiss_kappa,
    fleiss_kappa_one_vs_rest,
    pairwise_agreement,
    pairwise_agreement_by_pair,
)

_LABELS = [
    ["A", "A", "A"],
    ["B", "B", "B"],
    ["A", "A", "B"],
    ["A", "B", "B"],
]
_RATER_NAMES = ["mini", "full", "sonnet"]


def test_pairwise_agreement_hand_verified() -> None:
    """8 of 12 total rater-pairs agree across the fixture (2/3)."""
    assert pairwise_agreement(_LABELS) == pytest.approx(2 / 3)


def test_pairwise_agreement_all_agree_is_one() -> None:
    """Unanimous items score full pairwise agreement."""
    assert pairwise_agreement([["A", "A", "A"], ["B", "B", "B"]]) == 1.0


def test_pairwise_agreement_by_pair_hand_verified() -> None:
    """Per-pair agreement matches the worked arithmetic (mini/full/sonnet)."""
    result = pairwise_agreement_by_pair(_LABELS, _RATER_NAMES)
    assert result[("mini", "full")] == pytest.approx(0.75)
    assert result[("mini", "sonnet")] == pytest.approx(0.5)
    assert result[("full", "sonnet")] == pytest.approx(0.75)


def test_fleiss_kappa_hand_verified() -> None:
    """The fixture's Fleiss' kappa is exactly 1/3 (worked arithmetic in the plan doc)."""
    result = fleiss_kappa(_LABELS, categories=("A", "B"))
    assert result.status == "ok"
    assert result.kappa == pytest.approx(1 / 3)
    assert result.n_items == 4


def test_fleiss_kappa_all_agree_with_category_variance_is_one() -> None:
    """Perfect agreement, but categories vary across items -> kappa=1.0 (not degenerate)."""
    labels = [["A", "A", "A"], ["B", "B", "B"], ["A", "A", "A"], ["B", "B", "B"]]
    result = fleiss_kappa(labels, categories=("A", "B"))
    assert result.status == "ok"
    assert result.kappa == pytest.approx(1.0)


def test_fleiss_kappa_zero_variance_is_undefined() -> None:
    """A single category everywhere makes chance-agreement 1.0 -> undefined, not 0/1."""
    labels = [["A", "A", "A"]] * 5
    result = fleiss_kappa(labels, categories=("A", "B"))
    assert result.status == "undefined_zero_variance"
    assert result.kappa is None


def test_fleiss_kappa_rejects_off_vocabulary_label() -> None:
    """A label outside the declared category set is a hard error, not silently ignored."""
    with pytest.raises(ValueError):
        fleiss_kappa([["A", "A", "C"]], categories=("A", "B"))


def test_fleiss_kappa_rejects_nonuniform_rater_count() -> None:
    """Every item must carry the same number of rater labels."""
    with pytest.raises(ValueError):
        fleiss_kappa([["A", "A", "A"], ["A", "A"]], categories=("A", "B"))


def test_fleiss_kappa_one_vs_rest_reports_prevalence() -> None:
    """One-vs-rest kappa also reports the target category's raw assignment count."""
    result = fleiss_kappa_one_vs_rest(_LABELS, target="A")
    assert result.n_positive == 6  # A appears 3+0+2+1=6 times total across the fixture


def test_build_iaa_report_flags_split_items() -> None:
    """Any item without unanimous labels is listed as a disagreement."""
    report = build_iaa_report(
        rater_labels=_LABELS,
        item_ids=["case1::E1", "case2::E2", "case3::E3", "case4::E4"],
        rater_names=_RATER_NAMES,
        categories=("A", "B"),
    )
    assert report.disagreements == ["case3::E3", "case4::E4"]
    assert set(report.per_type) == {"A", "B"}
    assert report.by_rater_pair[("mini", "full")] == pytest.approx(0.75)
