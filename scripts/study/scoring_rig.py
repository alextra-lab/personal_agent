"""ADR-0114 AC-4 scoring rig (FRE-840, D7/D8).

Recall@20/nDCG@20, paired comparison, effect size + 95% CI, nDCG
non-inferiority test. Reuses the pure metric core from ``scripts/eval/fre435_memory_recall/metrics.py``
(``recall_at_k``/``ndcg_at_k``) rather than reimplementing it — this rig only
adds what does not already exist in the repo: a paired-comparison layer over
two systems' per-cue scores.

**Statistical test choice.** ADR-0114 AC-4(iii) pre-registers either a
Wilcoxon signed-rank test or a paired bootstrap CI as acceptable
(``docs/architecture_decisions/ADR-0114-heterarchical-associative-memory-study.md``,
AC-4). This module implements the **percentile bootstrap CI** option:
``scipy`` (which would supply Wilcoxon) is not a dependency anywhere in this
repo, and the bootstrap CI is explicitly named as an equally acceptable,
pre-registered test — no new dependency is justified for one use case.

**None-handling.** ``recall_at_k``/``ndcg_at_k`` return ``None`` when a cue's
gold set is empty (the reused metrics' own convention — see ``metrics.py``).
Any cue pair where either side is ``None`` for the metric being compared is
excluded from the paired diff, and the excluded count is reported on the
result rather than silently dropped (a broken/empty-gold cue must not
silently narrow the effective sample).

**This module builds the mechanism, not the verdict.** ``evaluate_ac4``
combines all of AC-4(i)-(iii) into one verdict function, but the
pre-registered margins (``relative_bar``, ``absolute_floor``, ``ndcg_margin``)
and the formal act of recording a verdict belong to FRE-843 (the v0-synthesis
seam ticket) — this rig supplies correct, tested primitives for that ticket
to call once arm C (FRE-842) and the frozen cue/gold set (FRE-841) exist.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from scripts.eval.fre435_memory_recall.metrics import ndcg_at_k, recall_at_k


@dataclass(frozen=True)
class CueScore:
    """One cue's scored row — the unit of "the scored baseline table".

    Attributes:
        cue_id: Stable cue identifier.
        recall_at_k: Recall@k, or ``None`` when the cue's gold set is empty.
        ndcg_at_k: nDCG@k, or ``None`` when the cue's gold set is empty.
    """

    cue_id: str
    recall_at_k: float | None
    ndcg_at_k: float | None


def score_cue(cue_id: str, retrieved: Sequence[str], relevant: set[str], k: int = 20) -> CueScore:
    """Score one cue's retrieved ranking against its gold set.

    Args:
        cue_id: Stable cue identifier.
        retrieved: Ordered retrieved ids (best first), namespaced (e.g. ``entity:...``).
        relevant: The gold id set for this cue.
        k: Cut-off rank (default 20, per AC-4).

    Returns:
        The cue's :class:`CueScore` (``None`` fields when ``relevant`` is empty).

    Raises:
        ValueError: If ``k`` is not a positive cut-off.
    """
    return CueScore(
        cue_id=cue_id,
        recall_at_k=recall_at_k(retrieved, relevant, k),
        ndcg_at_k=ndcg_at_k(retrieved, relevant, k),
    )


@dataclass(frozen=True)
class BootstrapCI:
    """A percentile bootstrap confidence interval over paired differences.

    Attributes:
        point_estimate: The observed mean paired difference (the effect size).
        ci_low: Lower bound of the ``1 - alpha`` confidence interval.
        ci_high: Upper bound of the ``1 - alpha`` confidence interval.
        n_resamples: Number of bootstrap resamples drawn.
    """

    point_estimate: float
    ci_low: float
    ci_high: float
    n_resamples: int


def paired_bootstrap_ci(
    diffs: Sequence[float],
    *,
    n_resamples: int = 10000,
    alpha: float = 0.05,
    rng: random.Random | None = None,
) -> BootstrapCI:
    """Percentile bootstrap CI over paired differences (the effect-size primitive).

    Resamples ``diffs`` with replacement ``n_resamples`` times, takes the mean
    of each resample, and reports the ``alpha/2``/``1 - alpha/2`` percentiles
    of that resampled-mean distribution.

    Args:
        diffs: Per-cue paired differences (e.g. study recall@k - baseline recall@k).
        n_resamples: Number of bootstrap resamples.
        alpha: Significance level (default 0.05 -> a 95% CI).
        rng: Optional seeded :class:`random.Random` for deterministic tests.
            A fresh, unseeded generator is used when omitted.

    Returns:
        The observed mean and its bootstrap confidence interval.

    Raises:
        ValueError: If ``diffs`` is empty.
    """
    if not diffs:
        raise ValueError("diffs must be non-empty")
    generator = rng if rng is not None else random.Random()
    n = len(diffs)
    point_estimate = statistics.fmean(diffs)
    resampled_means = []
    for _ in range(n_resamples):
        resample = [diffs[generator.randrange(n)] for _ in range(n)]
        resampled_means.append(statistics.fmean(resample))
    resampled_means.sort()
    low_idx = int((alpha / 2) * n_resamples)
    high_idx = int((1 - alpha / 2) * n_resamples) - 1
    high_idx = min(high_idx, n_resamples - 1)
    return BootstrapCI(
        point_estimate=point_estimate,
        ci_low=resampled_means[low_idx],
        ci_high=resampled_means[high_idx],
        n_resamples=n_resamples,
    )


@dataclass(frozen=True)
class SignificanceResult:
    """Whether a paired bootstrap CI excludes zero (the AC-4(iii) significance test).

    Attributes:
        ci: The underlying bootstrap CI.
        significant: Whether the CI excludes 0 (``ci_low > 0`` or ``ci_high < 0``).
        excluded_count: Cue pairs dropped for a ``None`` metric on either side.
    """

    ci: BootstrapCI
    significant: bool
    excluded_count: int


def paired_significance(
    diffs: Sequence[float],
    *,
    n_resamples: int = 10000,
    alpha: float = 0.05,
    rng: random.Random | None = None,
) -> SignificanceResult:
    """The ADR's named "paired bootstrap CI" significance test (AC-4(iii)).

    Args:
        diffs: Per-cue paired differences.
        n_resamples: Number of bootstrap resamples.
        alpha: Significance level (default 0.05 -> a 95% CI).
        rng: Optional seeded generator for deterministic tests.

    Returns:
        The CI and whether it excludes zero.
    """
    ci = paired_bootstrap_ci(diffs, n_resamples=n_resamples, alpha=alpha, rng=rng)
    significant = ci.ci_low > 0.0 or ci.ci_high < 0.0
    return SignificanceResult(ci=ci, significant=significant, excluded_count=0)


@dataclass(frozen=True)
class NonInferiorityResult:
    """The AC-4 nDCG non-inferiority check: does the lower CI bound clear ``-margin``?

    Attributes:
        ci: The underlying bootstrap CI over (study - baseline) nDCG diffs.
        margin: The pre-registered non-inferiority margin (delta).
        holds: Whether ``ci.ci_low > -margin``.
        excluded_count: Cue pairs dropped for a ``None`` metric on either side.
    """

    ci: BootstrapCI
    margin: float
    holds: bool
    excluded_count: int


def non_inferiority_test(
    diffs: Sequence[float],
    margin: float,
    *,
    n_resamples: int = 10000,
    alpha: float = 0.05,
    rng: random.Random | None = None,
) -> NonInferiorityResult:
    """AC-4's nDCG non-inferiority test: lower 95% CI bound of the diff > ``-margin``.

    Args:
        diffs: Per-cue paired differences (study - baseline).
        margin: The pre-registered non-inferiority margin (delta), non-negative.
        n_resamples: Number of bootstrap resamples.
        alpha: Significance level (default 0.05 -> a 95% CI).
        rng: Optional seeded generator for deterministic tests.

    Returns:
        The CI, margin, and whether non-inferiority holds.
    """
    ci = paired_bootstrap_ci(diffs, n_resamples=n_resamples, alpha=alpha, rng=rng)
    return NonInferiorityResult(ci=ci, margin=margin, holds=ci.ci_low > -margin, excluded_count=0)


def _paired_diffs(
    baseline: Sequence[CueScore],
    study: Sequence[CueScore],
    metric: Literal["recall_at_k", "ndcg_at_k"],
) -> tuple[list[float], int]:
    """Build paired (study - baseline) diffs for one metric, dropping ``None`` pairs.

    Args:
        baseline: Baseline system's per-cue scores, in a fixed cue order.
        study: Study system's per-cue scores, in the SAME cue order.
        metric: Which :class:`CueScore` field to diff.

    Returns:
        ``(diffs, excluded_count)`` — the paired diffs with any cue whose
        metric is ``None`` on either side dropped, and how many were dropped.

    Raises:
        ValueError: If the two sequences differ in length or cue-id order —
            a silent misalignment would corrupt every downstream statistic.
    """
    if len(baseline) != len(study):
        raise ValueError(
            f"baseline ({len(baseline)} cues) and study ({len(study)} cues) length mismatch"
        )
    diffs: list[float] = []
    excluded = 0
    for b, s in zip(baseline, study, strict=True):
        if b.cue_id != s.cue_id:
            raise ValueError(f"cue_id mismatch at paired position: {b.cue_id!r} != {s.cue_id!r}")
        b_val = getattr(b, metric)
        s_val = getattr(s, metric)
        if b_val is None or s_val is None:
            excluded += 1
            continue
        diffs.append(s_val - b_val)
    return diffs, excluded


def paired_recall_comparison(
    baseline: Sequence[CueScore],
    study: Sequence[CueScore],
    *,
    n_resamples: int = 10000,
    alpha: float = 0.05,
    rng: random.Random | None = None,
) -> SignificanceResult:
    """AC-4(iii): the paired Recall@k significance test between two systems.

    Args:
        baseline: Baseline system's per-cue scores, in a fixed cue order.
        study: Study system's per-cue scores, in the SAME cue order.
        n_resamples: Number of bootstrap resamples.
        alpha: Significance level (default 0.05 -> a 95% CI).
        rng: Optional seeded generator for deterministic tests.

    Returns:
        The significance result over Recall@k, with ``excluded_count`` set.

    Raises:
        ValueError: On cue-id/length mismatch (see :func:`_paired_diffs`), or
            if every cue pair was excluded (nothing left to test).
    """
    diffs, excluded = _paired_diffs(baseline, study, "recall_at_k")
    if not diffs:
        raise ValueError("no cue pairs with a non-empty gold set on both sides")
    result = paired_significance(diffs, n_resamples=n_resamples, alpha=alpha, rng=rng)
    return SignificanceResult(ci=result.ci, significant=result.significant, excluded_count=excluded)


def paired_ndcg_non_inferiority(
    baseline: Sequence[CueScore],
    study: Sequence[CueScore],
    margin: float,
    *,
    n_resamples: int = 10000,
    alpha: float = 0.05,
    rng: random.Random | None = None,
) -> NonInferiorityResult:
    """AC-4's nDCG non-inferiority test between two systems' per-cue scores.

    Args:
        baseline: Baseline system's per-cue scores, in a fixed cue order.
        study: Study system's per-cue scores, in the SAME cue order.
        margin: The pre-registered non-inferiority margin (delta).
        n_resamples: Number of bootstrap resamples.
        alpha: Significance level (default 0.05 -> a 95% CI).
        rng: Optional seeded generator for deterministic tests.

    Returns:
        The non-inferiority result over nDCG@k, with ``excluded_count`` set.

    Raises:
        ValueError: On cue-id/length mismatch, or if every pair was excluded.
    """
    diffs, excluded = _paired_diffs(baseline, study, "ndcg_at_k")
    if not diffs:
        raise ValueError("no cue pairs with a non-empty gold set on both sides")
    result = non_inferiority_test(diffs, margin, n_resamples=n_resamples, alpha=alpha, rng=rng)
    return NonInferiorityResult(
        ci=result.ci, margin=margin, holds=result.holds, excluded_count=excluded
    )


@dataclass(frozen=True)
class Ac4Verdict:
    """The full AC-4(i)-(iii) + nDCG non-inferiority combined verdict.

    Attributes:
        relative_lift: study_mean / baseline_mean over Recall@k (``None`` if
            baseline mean is 0 — undefined, not a false pass).
        relative_bar: The pre-registered relative bar (default 1.10).
        absolute_lift: study_mean - baseline_mean over Recall@k.
        absolute_floor: The pre-registered absolute floor.
        recall_significance: The paired Recall@k significance result.
        ndcg_non_inferiority: The paired nDCG@k non-inferiority result.
        passes: Whether ALL bars hold. ``None`` inputs never vacuously pass.
    """

    relative_lift: float | None
    relative_bar: float
    absolute_lift: float | None
    absolute_floor: float
    recall_significance: SignificanceResult
    ndcg_non_inferiority: NonInferiorityResult
    passes: bool


def evaluate_ac4(
    baseline: Sequence[CueScore],
    study: Sequence[CueScore],
    *,
    relative_bar: float,
    absolute_floor: float,
    ndcg_margin: float,
    n_resamples: int = 10000,
    alpha: float = 0.05,
    rng: random.Random | None = None,
) -> Ac4Verdict:
    """Combine AC-4(i)-(iii) and the nDCG non-inferiority check into one verdict.

    The pre-registered bars (``relative_bar``, ``absolute_floor``,
    ``ndcg_margin``) are supplied by the caller (FRE-843 owns fixing these
    before any scoring, per AC-4) — this function only combines them
    correctly and never lets a missing input silently pass.

    Args:
        baseline: Baseline system's per-cue scores, in a fixed cue order.
        study: Study system's per-cue scores, in the SAME cue order.
        relative_bar: Required study/baseline Recall@k ratio (AC-4(i), default 1.10).
        absolute_floor: Required study - baseline Recall@k floor (AC-4(ii)).
        ndcg_margin: The nDCG non-inferiority margin (delta).
        n_resamples: Number of bootstrap resamples.
        alpha: Significance level (default 0.05 -> a 95% CI).
        rng: Optional seeded generator for deterministic tests.

    Returns:
        The combined :class:`Ac4Verdict`.

    Raises:
        ValueError: On cue-id/length mismatch, or if every pair was excluded.
    """
    recall_diffs, excluded = _paired_diffs(baseline, study, "recall_at_k")
    if not recall_diffs:
        raise ValueError("no cue pairs with a non-empty gold set on both sides")

    baseline_recalls = [b.recall_at_k for b in baseline if b.recall_at_k is not None]
    study_recalls = [s.recall_at_k for s in study if s.recall_at_k is not None]
    baseline_mean = statistics.fmean(baseline_recalls) if baseline_recalls else None
    study_mean = statistics.fmean(study_recalls) if study_recalls else None

    relative_lift = (
        study_mean / baseline_mean
        if baseline_mean is not None and baseline_mean > 0 and study_mean is not None
        else None
    )
    absolute_lift = (
        study_mean - baseline_mean if baseline_mean is not None and study_mean is not None else None
    )

    # Reuse recall_diffs/excluded computed above rather than recomputing them
    # inside paired_recall_comparison (codex review: avoid the duplicate
    # _paired_diffs walk on every evaluate_ac4 call).
    sig = paired_significance(recall_diffs, n_resamples=n_resamples, alpha=alpha, rng=rng)
    recall_sig = SignificanceResult(ci=sig.ci, significant=sig.significant, excluded_count=excluded)
    ndcg_ni = paired_ndcg_non_inferiority(
        baseline, study, ndcg_margin, n_resamples=n_resamples, alpha=alpha, rng=rng
    )

    passes = (
        relative_lift is not None
        and relative_lift >= relative_bar
        and absolute_lift is not None
        and absolute_lift >= absolute_floor
        and recall_sig.significant
        and ndcg_ni.holds
    )

    return Ac4Verdict(
        relative_lift=relative_lift,
        relative_bar=relative_bar,
        absolute_lift=absolute_lift,
        absolute_floor=absolute_floor,
        recall_significance=recall_sig,
        ndcg_non_inferiority=ndcg_ni,
        passes=passes,
    )
