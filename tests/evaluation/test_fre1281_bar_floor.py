"""FRE-1281 — the bars reject known-broken implementations (ADR-0138 AC-7 floor principle).

Preregistration stops a bar being tuned to a result. It does not stop a bar being
vacuous: 0% per-class recall, recorded in advance, satisfies the timing rule perfectly and
means nothing. ADR-0138 therefore requires every bar to be "demonstrated to reject a
deliberately broken baseline — a bar that a known-broken implementation would pass is not
a bar."

This file is that demonstration, run against the real committed corpus rather than a
fixture. Each ``rejects_baselines`` entry in ``bars.py`` is a claim, and each claim is
checked here.

A codex plan review is why two of these assertions are worth more than they look: it
showed that ``entity_triggered`` and ``accept_all`` were credited with failing bars they
were not actually guaranteed to fail. Both are now secured by corpus load-time invariants
(``test_fre1281_corpus.py``), and these tests are what would notice if that stopped being
true.
"""

from __future__ import annotations

import pytest
from scripts.eval.fre1281_span_extraction.bars import (
    BaselineName,
    all_bars,
    bars_naming,
    extractor_bars,
)
from scripts.eval.fre1281_span_extraction.baselines import BASELINES, run_baseline
from scripts.eval.fre1281_span_extraction.corpus import SpanClass, load_corpus
from scripts.eval.fre1281_span_extraction.metrics import score_document
from scripts.eval.fre1281_span_extraction.report import aggregate, class_coverage_gaps

CORPUS = load_corpus()


def _report(name: BaselineName):
    """Score one baseline over the whole corpus."""
    pairs = run_baseline(name, CORPUS)
    return aggregate([score_document(doc, pred) for doc, pred in pairs])


@pytest.mark.parametrize(
    "baseline",
    [name for name in BaselineName if name is not BaselineName.ORACLE],
)
def test_baseline_fails_every_bar_that_names_it(baseline: BaselineName) -> None:
    """Each broken baseline fails every bar claiming to reject it.

    This is the floor principle made executable. A bar listing a baseline it does not
    actually reject is decoration, and would let a broken extractor through while looking
    rigorous.
    """
    report = _report(baseline)
    claimed = bars_naming(baseline)
    assert claimed, f"{baseline.value} is not named by any bar — it demonstrates nothing"

    failed = {bar.key for bar in report.unmet_bars()}
    survived = [bar.key for bar in claimed if bar.key not in failed]
    assert not survived, (
        f"{baseline.value} PASSED bars that claim to reject it: {survived}. Those bars "
        f"do not bite, whatever their preregistered value says."
    )


def test_oracle_passes_every_bar() -> None:
    """Positive control: the bars are strict, not merely unsatisfiable.

    Without this, a bar set of 1.01 everywhere would ace every rejection test above while
    measuring nothing at all.
    """
    report = _report(BaselineName.ORACLE)
    unmet_keys = [bar.key for bar in report.unmet_bars()]
    assert not unmet_keys, f"the oracle failed {unmet_keys} — those bars are unsatisfiable"


def test_oracle_leaves_no_class_unmeasured() -> None:
    """Every preregistered metric has a denominator in this corpus.

    ADR-0138 AC-7 fails if "any class is unreported", so a metric that can never be
    computed is a hole in the criteria rather than a neutral absence.
    """
    report = _report(BaselineName.ORACLE)
    assert not class_coverage_gaps(report)


def test_entity_triggered_scores_zero_on_bare_predicates() -> None:
    """The specific failure D1's inversion exists to catch, asserted directly.

    An entity-or-figure trigger posts a respectable overall recall, which is exactly why
    an overall bar alone would have let the rejected draft of D1 through. The per-class
    bar is what catches it.
    """
    report = _report(BaselineName.ENTITY_TRIGGERED)
    bare = report.metrics[f"recall.class.{SpanClass.FACTUAL_BARE_PREDICATE.value}"]
    assert bare == 0.0, f"expected zero bare-predicate recall, got {bare}"

    # The point is that the overall figure CONCEALS the hole, not that it looks good in
    # absolute terms — how good it looks is an artefact of corpus composition, and
    # asserting a threshold on it would be a number tuned to pass.
    overall = report.metrics["recall.overall"]
    assert overall is not None and overall > bare, (
        f"overall recall {overall} is not above bare-predicate recall {bare} — then the "
        f"overall figure would not be hiding anything, and this baseline would not "
        f"demonstrate why per-class bars are needed"
    )


def test_fence_trusting_scores_zero_where_fencing_is_trusted() -> None:
    """D1: "The exemption attaches to code, not to fencing."."""
    report = _report(BaselineName.FENCE_TRUSTING)
    for span_class in (SpanClass.PROSE_IN_FENCE, SpanClass.NL_IN_CODE):
        observed = report.metrics[f"recall.class.{span_class.value}"]
        assert observed == 0.0, f"{span_class.value} recall {observed}, expected 0.0"


def test_accept_all_fails_precision_and_sweeps_every_exempt_class() -> None:
    """Blocking legitimate generation must not score well."""
    report = _report(BaselineName.ACCEPT_ALL)
    precision = report.metrics["precision.overall"]
    assert precision is not None and precision < 0.80

    for key, value in report.metrics.items():
        if key.startswith("fp_rate.class."):
            assert value == 1.0, f"{key} = {value}, expected every exempt span swept in"


def test_null_extractor_fails_recall_and_decomposition() -> None:
    """Recognising nothing must fail loudly, including on segmentation.

    Boundary F1 comes back **unmeasured** rather than 0.0, because precision over zero
    predictions is genuinely undefined. That is the fail-safe reading and the bar treats
    it as unmet — ADR-0138 AC-7 fails when a class is unreported, so silence is not an
    abstention. Asserting 0.0 here would have demanded the metric lie about a denominator
    it did not have.
    """
    report = _report(BaselineName.NULL)
    assert report.metrics["recall.overall"] == 0.0
    assert report.metrics["decomposition.boundary_f1"] is None

    failed = {bar.key for bar in report.unmet_bars()}
    assert "decomposition.boundary_f1" in failed
    assert "recall.overall" in failed


def test_every_bar_carries_a_justification() -> None:
    """An unjustified bar is the vacuous kind the floor principle exists to catch."""
    for bar in all_bars():
        assert bar.justification.strip(), f"{bar.key} carries no justification"


def test_every_extractor_bar_names_a_baseline() -> None:
    """No extractor bar may float free of a demonstration that it bites."""
    unnamed = [bar.key for bar in extractor_bars() if not bar.rejects_baselines]
    assert not unnamed, f"bars with nothing to reject: {unnamed}"


def test_every_baseline_is_exercised() -> None:
    """The baseline registry and the enum cannot drift apart silently."""
    assert set(BASELINES) == set(BaselineName)
