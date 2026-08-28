"""The corpus and the scorer behind AC-3 and AC-6 (FRE-1286).

Pure core only — no model. What these pin is that the *instrument* is sound: that a
mislabelled case is caught at load, that the classes AC-3 names are actually populated, and
that the scorer cannot be aced by a degenerate judge. A metric that a reject-everything
judge passes is not a measurement of anything.
"""

from __future__ import annotations

import pytest
from scripts.eval.fre1286_entailment.corpus import (
    CaseClass,
    CorpusError,
    Partition,
    load_corpus,
    partitioned,
)
from scripts.eval.fre1286_entailment.metrics import BARS, render, score


def test_the_committed_corpus_loads_and_validates() -> None:
    """The shipped file is the thing the harness will run; it must parse here."""
    cases = load_corpus()
    assert len(cases) >= 30
    assert len({case.id for case in cases}) == len(cases)


def test_every_class_ac3_names_is_populated_in_both_partitions() -> None:
    """AC-3's classes cannot be measured from an empty set.

    A corpus that happened to hold no contradiction cases would report a detection rate of
    zero-over-zero and, depending on the arithmetic, either crash or quietly read as 0.0 —
    neither of which is "contradiction is detected".
    """
    cases = load_corpus()
    for partition in Partition:
        present = {case.case_class for case in partitioned(cases, partition)}
        assert CaseClass.CONTRADICTED in present
        assert CaseClass.QUANTIFIER_REVERSAL in present
        assert CaseClass.SUPPORTED in present
        assert CaseClass.SILENT in present
        assert CaseClass.IMPLICITLY_REFUTED in present


def test_a_silent_case_cannot_expect_contradiction() -> None:
    """FRE-1301 AC-1: the boundary between `silent` and `implicitly_refuted` is mechanical.

    `silent` means the passage entails neither the claim nor its negation, so its expected
    verdict is always `not_supported` — never `contradicted`.
    """
    bad = (
        "cases:\n"
        "  - id: x\n"
        "    class: silent\n"
        "    partition: dev\n"
        "    expected: contradicted\n"
        "    claim: c\n"
        "    passage: p\n"
        "    note: n\n"
    )
    with pytest.raises(CorpusError, match="cannot expect"):
        load_corpus(_written(bad))


def test_an_implicitly_refuted_case_cannot_expect_not_supported() -> None:
    """FRE-1301 AC-1: `implicitly_refuted` means the passage entails the negation.

    Its expected verdict is always `contradicted` — the same verdict `contradicted` cases
    expect, which is the whole point of the split: both are refutation, one plainly worded
    and one inferred, and the class distinguishes them for telemetry without disagreeing
    with the judge about what the correct answer is.
    """
    bad = (
        "cases:\n"
        "  - id: x\n"
        "    class: implicitly_refuted\n"
        "    partition: dev\n"
        "    expected: not_supported\n"
        "    claim: c\n"
        "    passage: p\n"
        "    note: n\n"
    )
    with pytest.raises(CorpusError, match="cannot expect"):
        load_corpus(_written(bad))


_PREVIOUSLY_SCORED_HELDOUT_IDS = frozenset(
    {
        "sup-evaluative-grounded",
        "sup-negation-matched",
        "sup-unit-variant",
        "sup-list-membership",
        "sup-temporal",
        "sup-degree-matched",
        "ns-plan-not-fact",
        "ns-scope-shift",
        "ns-reversed-direction",
        "ns-absent-attribute",
        "ns-future-tense",
        "con-attribution-flip",
        "con-exclusion",
        "con-identity",
        "con-recall",
        "con-order",
        "quant-all-for-some",
        "quant-only",
        "quant-few",
        "quant-generic",
    }
)
"""The 20 case ids FRE-1286 scored as `heldout` on 2026-08-26. Frozen here as a regression
guard for FRE-1301 AC-2: none of these may appear in the corpus's `heldout` partition again.
"""


def test_the_fresh_heldout_partition_reuses_no_case_scored_2026_08_26() -> None:
    """FRE-1301 AC-2: a partition already scored cannot serve as held-out again.

    Checking only that these ids are absent from ``heldout`` would also pass if the corpus
    had simply deleted them — which is not what FRE-1301 did and would not be a fresh
    partition either. Asserting each one is still present, in ``dev``, is what proves the
    repartitioning actually happened rather than a silent loss of coverage.
    """
    cases = load_corpus()
    by_id = {case.id: case for case in cases}
    missing = _PREVIOUSLY_SCORED_HELDOUT_IDS - by_id.keys()
    assert not missing, f"previously-scored cases dropped from the corpus: {sorted(missing)}"
    for case_id in _PREVIOUSLY_SCORED_HELDOUT_IDS:
        assert by_id[case_id].partition is Partition.DEV, (
            f"{case_id} was scored heldout on 2026-08-26 and must not be heldout again"
        )


def test_a_mislabelled_case_is_refused_at_load() -> None:
    """A supported case expecting a rejection is a labelling error, not a hard case."""
    bad = (
        "cases:\n"
        "  - id: x\n"
        "    class: supported\n"
        "    partition: dev\n"
        "    expected: contradicted\n"
        "    claim: c\n"
        "    passage: p\n"
        "    note: n\n"
    )
    with pytest.raises(CorpusError, match="cannot expect"):
        load_corpus(_written(bad))


def test_a_duplicate_id_is_refused() -> None:
    """Ids name failures in the handoff; two cases sharing one hides a regression."""
    bad = (
        "cases:\n"
        "  - {id: x, class: supported, partition: dev, expected: supported, "
        "claim: c, passage: p, note: n}\n"
        "  - {id: x, class: supported, partition: dev, expected: supported, "
        "claim: c, passage: p, note: n}\n"
    )
    with pytest.raises(CorpusError, match="duplicate"):
        load_corpus(_written(bad))


def _written(text: str):  # type: ignore[no-untyped-def]
    """Write a corpus fixture to a temp file and return its path."""
    import tempfile
    from pathlib import Path

    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    handle.write(text)
    handle.close()
    return Path(handle.name)


# ── The scorer ──────────────────────────────────────────────────────────────────────


def test_a_reject_everything_judge_fails_the_bars() -> None:
    """The trivial judge scores 1.0 on three of four classes. It must still fail.

    Answering ``not_supported`` to everything detects every non-supporting source, which
    is what makes AC-1 alone insufficient. The false-rejection rate is the arm that catches
    it, and in production it is the expensive error: under D4 each one costs a refusal the
    user did not deserve.
    """
    cases = load_corpus()
    report = score(cases, {case.id: "not_supported" for case in cases})

    results = report.bar_results()
    assert results["false_rejection_rate"][2] is False
    assert results["accuracy"][2] is False


def test_a_perfect_judge_meets_every_bar() -> None:
    """The bars must be reachable, or they are not bars but a permanent failure."""
    cases = load_corpus()
    report = score(cases, {case.id: case.expected for case in cases})

    assert all(met for _, _, met in report.bar_results().values())
    assert report.accuracy == 1.0
    assert report.false_rejection_rate == 0.0


def test_a_missing_verdict_counts_as_undecided_not_as_absent() -> None:
    """A call the harness could not complete is the judge failing to answer.

    Dropping it from the denominator would flatter whatever did answer — the run would get
    *better* the more calls failed.
    """
    cases = load_corpus()
    verdicts = {case.id: case.expected for case in cases[2:]}
    report = score(cases, verdicts)

    assert report.total == len(cases)
    assert report.undecided_rate == pytest.approx(2 / len(cases))
    assert report.accuracy < 1.0


def test_contradiction_and_non_support_are_scored_apart() -> None:
    """A judge that calls every refutation "not supported" fails AC-3's contradiction bar.

    Which is the point of keeping the verdicts distinct: collapsing them would let a judge
    blind to the ADR's named residue read as competent.
    """
    cases = load_corpus()
    verdicts = {
        case.id: ("not_supported" if case.expected == "contradicted" else case.expected)
        for case in cases
    }
    report = score(cases, verdicts)

    assert report.bar_results()["detection_contradicted"][2] is False
    assert report.by_class[CaseClass.SUPPORTED].rate == 1.0


def test_bars_are_declared_for_every_reported_figure() -> None:
    """No figure is reported without something it had to beat."""
    report = score(load_corpus(), {})
    assert set(report.bar_results()) == set(BARS)
    assert "FAIL" in render(report)
