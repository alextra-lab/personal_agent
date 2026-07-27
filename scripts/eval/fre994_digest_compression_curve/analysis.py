"""Endpoints, intervals and the precommitted decision rule (FRE-994 §4.2, §6).

Everything that turns measurements into an answer lives here, and every threshold it
applies was fixed before any spend. The module is deliberately capable of returning
``inconclusive``: a study that can only produce a number will produce one whether or not
the data supports it, which is how the constant this ticket exists to replace got set.

Three guards, all precommitted:

* **Marginal, not absolute** — the generator omits things even unbounded, so the quantity
  reported is ΔL(T) = L(T) − L(unbounded), not L(T).
* **Monotonicity is a precondition** — a tighter bound showing *lower* loss than a looser
  one is not a compression curve, and the rule refuses to select from that region rather
  than rewarding the noise with the smallest bound.
* **Selection stability** — the rule takes a minimum over arms on a small sample, which is
  optimistic by construction. The selected arm must survive re-selection in a majority of
  bootstrap replicates or the answer is ``inconclusive``.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from typing import Any

#: §6.1 — ΔL at or below this, with §6's interval condition, clears the loss gate.
DELTA_L_MAX = 0.10

#: §6.1 — the upper end of ΔL's 90% paired-bootstrap interval must also clear this.
DELTA_L_UPPER_MAX = 0.20

#: §6 — fraction of an arm's content-bearing digests that must render within the bound
#: without truncation. A bound the generator cannot hit is a rejection rate, not a bound.
REACHABILITY_MIN = 0.90

#: §6.2 — a tighter arm may not show lower loss than a looser one by more than one
#: session's worth. Set from the sample size, so it is 1/N rather than a taste.
MONOTONICITY_SLACK_SESSIONS = 1

#: §6.3 — bootstrap replicates, and the share of them that must re-select the same arm.
BOOTSTRAP_REPLICATES = 10_000
SELECTION_STABILITY_MIN = 0.60

#: Fixed so the reported interval is reproducible from the committed write-up.
BOOTSTRAP_SEED = 994

#: Outcomes whose payload never parsed. Loud failures — they leave the session dirty and
#: retryable, unlike an empty digest, which returns GENERATED and marks it clean forever.
_UNUSABLE_OUTCOMES = frozenset({"invalid_json", "contract_drift", "provider_error"})


@dataclass(frozen=True)
class SessionOutcome:
    """One session's result at one arm.

    Attributes:
        session_id: The session.
        arm: The arm name.
        lost_a_conclusion: True when the digest omitted at least one consequential
            conclusion. None when the session was not judged at this arm.
        rendered_tokens: The consumer-facing token count, or None when unusable.
        within_bound: Whether the digest rendered within the arm's stated maximum.
        content_bearing: Whether any slot carried an item.
        truncated: Whether the reply was cut off at the call ceiling.
        outcome: The classifier's verdict (``generate.OUTCOMES``). Carried so the
            empty rate can count *empty* digests and not every non-content-bearing
            reply — a contract-drift row is a parse failure, which is loud, and folding
            it into the empty rate overstates the silent class that marks a session
            clean and is never retried.
    """

    session_id: str
    arm: str
    lost_a_conclusion: bool | None
    rendered_tokens: int | None
    within_bound: bool
    content_bearing: bool
    truncated: bool
    outcome: str = "ok"


@dataclass(frozen=True)
class ArmResult:
    """Everything the decision rule reads for one arm.

    Attributes:
        arm: The arm name.
        n_judged: Sessions carrying a loss verdict at this arm.
        loss_rate: L(T).
        delta_loss: ΔL(T), the excess over the unbounded arm.
        delta_loss_ci: 90% paired-bootstrap interval on ΔL(T).
        reachability: Share of content-bearing digests rendering within the bound
            without truncation.
        content_bearing_rate: Share of all calls that produced any item.
        empty_rate: Share that parsed but filled no slot — the failure that marks a
            session clean and is never retried. Counted from the classifier's own
            ``empty`` verdict, not from "not content-bearing", which also catches parse
            failures.
        unusable_rate: Share whose payload did not parse at all — contract drift,
            invalid JSON, or a provider error. Loud failures, reported separately so
            they can never be read as the silent kind.
        truncation_rate: Share cut off at the call ceiling.
        rendered_p50: Median rendered tokens over usable digests.
        rendered_p90: 90th percentile.
        rendered_max: Maximum — the all-pass threshold for this arm.
    """

    arm: str
    n_judged: int
    loss_rate: float
    delta_loss: float
    delta_loss_ci: tuple[float, float]
    reachability: float
    content_bearing_rate: float
    empty_rate: float
    unusable_rate: float
    truncation_rate: float
    rendered_p50: int | None
    rendered_p90: int | None
    rendered_max: int | None


@dataclass(frozen=True)
class Decision:
    """The rule's verdict, with everything needed to audit it.

    Attributes:
        selected_arm: The recommended arm, or None when inconclusive.
        inconclusive_reason: Why no arm was selected. Empty when one was.
        selection_frequency: Bootstrap re-selection share per arm.
        monotonicity_violations: Arm pairs whose loss ordering inverted beyond slack.
        optimism: Nominal ΔL of the selected arm minus the mean ΔL of whichever arm
            each replicate selected. Positive means the point estimate flatters.
    """

    selected_arm: str | None
    inconclusive_reason: str = ""
    selection_frequency: dict[str, float] = field(default_factory=dict)
    monotonicity_violations: list[tuple[str, str]] = field(default_factory=list)
    optimism: float | None = None


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(q * len(ordered)))
    return ordered[idx]


def _loss_rate(outcomes: list[SessionOutcome]) -> float:
    judged = [o for o in outcomes if o.lost_a_conclusion is not None]
    if not judged:
        return 0.0
    return sum(1 for o in judged if o.lost_a_conclusion) / len(judged)


def paired_loss_rates(
    outcomes: list[SessionOutcome], reference: list[SessionOutcome]
) -> tuple[float, float, int]:
    """L(T) and L(unbounded) over the sessions BOTH arms judged.

    Differencing two rates computed over different session sets is not a paired
    difference — it is two unpaired proportions subtracted, and it can put the point
    estimate outside its own paired interval. Sessions do drop out per-arm: a cell whose
    digest failed to parse carries no verdict, and it fails at one arm and not another.

    Args:
        outcomes: The arm's outcomes.
        reference: The reference arm's outcomes.

    Returns:
        The arm's rate, the reference's rate, and the size of the intersection.
    """
    ref_by_session = {o.session_id: o for o in reference if o.lost_a_conclusion is not None}
    pairs = [
        (o.lost_a_conclusion, ref_by_session[o.session_id].lost_a_conclusion)
        for o in outcomes
        if o.lost_a_conclusion is not None and o.session_id in ref_by_session
    ]
    if not pairs:
        return (0.0, 0.0, 0)
    n = len(pairs)
    return (sum(1 for a, _ in pairs if a) / n, sum(1 for _, b in pairs if b) / n, n)


def summarise_arm(
    outcomes: list[SessionOutcome],
    *,
    reference: list[SessionOutcome],
    bound: int | None,
    with_interval: bool = True,
) -> ArmResult:
    """Reduce one arm's per-session outcomes to the row the decision rule reads.

    Args:
        outcomes: Every call made on this arm.
        reference: The unbounded arm's outcomes, which ΔL is differenced against.
        bound: The arm's stated maximum, or None for the unbounded arm.
        with_interval: Compute the bootstrap interval. False inside a bootstrap
            replicate, where an interval on a replicate estimates nothing and the outer
            interval has already been applied once — it is also 99% of the runtime.

    Returns:
        The arm's summary row.
    """
    arm = outcomes[0].arm if outcomes else "unknown"
    judged = [o for o in outcomes if o.lost_a_conclusion is not None]
    loss = _loss_rate(outcomes)
    # Paired on both sides: the estimate the gate reads and the interval that describes
    # it must be the same estimand over the same sessions.
    paired_loss, paired_reference, n_paired = paired_loss_rates(outcomes, reference)
    delta = paired_loss - paired_reference if n_paired else 0.0

    content = [o for o in outcomes if o.content_bearing]
    reachable = [o for o in content if o.within_bound and not o.truncated]
    rendered = [o.rendered_tokens for o in outcomes if o.rendered_tokens]

    if not bound:
        # No bound to fall outside of.
        reachability = 1.0
    elif not content:
        # Fails CLOSED. An arm that produced no content at all has not demonstrated it
        # can hit its bound — reporting 1.0 here would let an arm whose every digest was
        # empty pass the reachability gate and be recommended.
        reachability = 0.0
    else:
        reachability = len(reachable) / len(content)

    return ArmResult(
        arm=arm,
        n_judged=len(judged),
        loss_rate=loss,
        delta_loss=delta,
        delta_loss_ci=paired_bootstrap_ci(outcomes, reference) if with_interval else (0.0, 0.0),
        reachability=reachability,
        content_bearing_rate=(len(content) / len(outcomes)) if outcomes else 0.0,
        empty_rate=(
            sum(1 for o in outcomes if o.outcome == "empty") / len(outcomes) if outcomes else 0.0
        ),
        unusable_rate=(
            sum(1 for o in outcomes if o.outcome in _UNUSABLE_OUTCOMES) / len(outcomes)
            if outcomes
            else 0.0
        ),
        truncation_rate=(sum(1 for o in outcomes if o.truncated) / len(outcomes))
        if outcomes
        else 0.0,
        rendered_p50=_percentile(rendered, 0.5),
        rendered_p90=_percentile(rendered, 0.9),
        rendered_max=max(rendered) if rendered else None,
    )


def paired_bootstrap_ci(
    outcomes: list[SessionOutcome],
    reference: list[SessionOutcome],
    *,
    replicates: int = BOOTSTRAP_REPLICATES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    """90% interval on ΔL, resampling **sessions** so the pairing survives.

    Every arm runs on the same sessions, so ΔL is a within-session difference. Resampling
    cells rather than sessions would break that: a session would enter a replicate at one
    arm and not the other, turning a paired difference into an unpaired one and widening
    the interval for the wrong reason.

    Args:
        outcomes: The arm's outcomes.
        reference: The unbounded arm's outcomes.
        replicates: Bootstrap replicates.
        seed: Fixed, so the published interval is reproducible.

    Returns:
        The 5th and 95th percentiles of the resampled ΔL.
    """
    ref_by_session = {o.session_id: o for o in reference if o.lost_a_conclusion is not None}
    paired = [
        (o.lost_a_conclusion, ref_by_session[o.session_id].lost_a_conclusion)
        for o in outcomes
        if o.lost_a_conclusion is not None and o.session_id in ref_by_session
    ]
    if not paired:
        return (0.0, 0.0)

    rng = random.Random(seed)
    n = len(paired)
    deltas: list[float] = []
    for _ in range(replicates):
        draw = [paired[rng.randrange(n)] for _ in range(n)]
        deltas.append(sum(a for a, _ in draw) / n - sum(b for _, b in draw) / n)
    deltas.sort()
    return (deltas[int(0.05 * replicates)], deltas[int(0.95 * replicates)])


def monotonicity_violations(
    rows: list[ArmResult], *, order: list[str], n_sessions: int
) -> list[tuple[str, str]]:
    """Arm pairs whose loss ordering inverted by more than one session's worth.

    A compression curve should not show a *tighter* bound losing *less* than a looser
    one. When it does, the ordering is noise, and selecting the tighter arm would reward
    that noise with the smallest bound — the failure mode §6.2 exists to stop.

    Args:
        rows: Per-arm summaries.
        order: Arm names from tightest to loosest.
        n_sessions: Sample size, which sets the slack at one session's worth.

    Returns:
        ``(tighter, looser)`` pairs that inverted.
    """
    slack = MONOTONICITY_SLACK_SESSIONS / n_sessions if n_sessions else 0.0
    # An unjudged arm has a loss rate of 0.0 by default, which would read as "loses
    # nothing" and manufacture an inversion against every judged arm — reporting a
    # non-monotone *curve* when the truth is a missing *measurement*.
    by_name = {r.arm: r for r in rows if r.n_judged}
    violations: list[tuple[str, str]] = []
    for i, tighter in enumerate(order):
        for looser in order[i + 1 :]:
            if tighter in by_name and looser in by_name:
                if by_name[tighter].loss_rate < by_name[looser].loss_rate - slack:
                    violations.append((tighter, looser))
    return violations


def _select(rows: list[ArmResult], *, order: list[str], check_interval: bool) -> str | None:
    """Apply §6's two conditions and return the smallest arm clearing both."""
    by_name = {r.arm: r for r in rows}
    for name in order:
        row = by_name.get(name)
        if row is None or row.n_judged == 0:
            continue
        if row.delta_loss > DELTA_L_MAX:
            continue
        if check_interval and row.delta_loss_ci[1] > DELTA_L_UPPER_MAX:
            continue
        if row.reachability < REACHABILITY_MIN:
            continue
        return name
    return None


def decide(
    outcomes_by_arm: dict[str, list[SessionOutcome]],
    *,
    order: list[str],
    reference_arm: str,
    bounds: dict[str, int | None],
    n_sessions: int,
    replicates: int = BOOTSTRAP_REPLICATES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[list[ArmResult], Decision]:
    """Run the full precommitted rule and report what it concluded.

    Args:
        outcomes_by_arm: Every arm's per-session outcomes.
        order: Arm names from tightest to loosest, excluding the reference.
        reference_arm: The unbounded arm.
        bounds: Each arm's stated maximum.
        n_sessions: Sample size.
        replicates: Bootstrap replicates for the stability check.
        seed: Fixed, so the published figures are reproducible.

    Returns:
        The per-arm table and the decision.
    """
    reference = outcomes_by_arm.get(reference_arm, [])
    rows = [
        summarise_arm(outcomes, reference=reference, bound=bounds.get(name))
        for name, outcomes in outcomes_by_arm.items()
        if outcomes
    ]

    if not any(r.n_judged for r in rows):
        # Distinguished from "no bound is safe", which is a finding about the digest.
        # This is a finding about the *instrument*: without a loss verdict the rule's
        # first condition was never evaluated, and reporting the §6 message here would
        # claim evidence the run does not have.
        return rows, Decision(
            selected_arm=None,
            inconclusive_reason=(
                "no arm carries a loss verdict, so §6's first condition was never "
                "evaluated. The delivery endpoint below stands on its own; the loss "
                "endpoint did not run, or its §4.3 validity gates failed and it is "
                "barred from selecting the bound"
            ),
        )

    reference_rows = [r for r in rows if r.arm == reference_arm]
    if not reference_rows or not reference_rows[0].n_judged:
        # ΔL is defined as an excess over the unbounded arm. Without it the difference
        # silently becomes an ABSOLUTE loss rate, defeating the module's own first
        # guard, and the paired interval collapses to a fake zero width.
        return rows, Decision(
            selected_arm=None,
            inconclusive_reason=(
                f"the reference arm {reference_arm!r} carries no loss verdict, so ΔL "
                "cannot be formed. An absolute loss rate would confuse 'the bound is "
                "too tight' with 'the generator is imperfect', which is the confusion "
                "the marginal endpoint exists to prevent"
            ),
        )

    violations = monotonicity_violations(rows, order=order, n_sessions=n_sessions)
    if violations:
        return rows, Decision(
            selected_arm=None,
            inconclusive_reason=(
                "loss ordering is non-monotone beyond one session's slack "
                f"({violations}) — the curve is not behaving as a compression curve, "
                "so §6.2 refuses to select from it"
            ),
            monotonicity_violations=violations,
        )

    nominal = _select(rows, order=order, check_interval=True)
    if nominal is None:
        return rows, Decision(
            selected_arm=None,
            inconclusive_reason=(
                "no tested bound satisfies both §6 conditions; the bound must come from "
                "the Phase-2 consumer, and the unbounded arm's achieved distribution is "
                "the floor it has to clear"
            ),
        )

    # Re-select from scratch on each replicate, so the frequency reports how often the
    # *rule* lands here — not how often this arm happens to look acceptable.
    sessions = sorted({o.session_id for o in reference})
    rng = random.Random(seed)
    picks: list[str | None] = []
    picked_deltas: list[float] = []
    for _ in range(replicates):
        drawn = [sessions[rng.randrange(len(sessions))] for _ in range(len(sessions))]
        counts: dict[str, int] = {}
        for sid in drawn:
            counts[sid] = counts.get(sid, 0) + 1
        replicate = {
            name: [o for o in arm_outcomes for _ in range(counts.get(o.session_id, 0))]
            for name, arm_outcomes in outcomes_by_arm.items()
        }
        # `with_interval=False`: a bootstrap interval computed on a bootstrap replicate
        # estimates nothing, the outer interval already applied it once, and computing
        # it here is 99% of this loop's runtime.
        replicate_rows = [
            summarise_arm(
                o,
                reference=replicate.get(reference_arm, []),
                bound=bounds.get(name),
                with_interval=False,
            )
            for name, o in replicate.items()
            if o
        ]
        # §6.3 says the FULL rule, including §6.2. Applying only `_select` would let a
        # replicate whose curve inverted still return its tightest passing arm, which
        # inflates that arm's re-selection share with exactly the noise §6.2 exists to
        # suppress — measured at ~14% of replicates on a monotone nominal curve, enough
        # to carry a bound across the 60% gate.
        if monotonicity_violations(replicate_rows, order=order, n_sessions=n_sessions):
            picks.append(None)
            continue
        pick = _select(replicate_rows, order=order, check_interval=False)
        picks.append(pick)
        if pick is not None:
            # The replicate's OWN ΔL for the arm it chose. Indexing the nominal table
            # here instead would make `optimism` a function of the pick distribution
            # alone — identically zero whenever the rule is stable, and structurally
            # incapable of detecting the selection bias it is named for.
            picked_deltas.extend(r.delta_loss for r in replicate_rows if r.arm == pick)

    frequency = {
        name: picks.count(name) / replicates for name in {p for p in picks if p is not None}
    }
    stability = frequency.get(nominal, 0.0)

    by_name = {r.arm: r for r in rows}
    optimism = (
        by_name[nominal].delta_loss - statistics.fmean(picked_deltas) if picked_deltas else None
    )

    if stability < SELECTION_STABILITY_MIN:
        return rows, Decision(
            selected_arm=None,
            inconclusive_reason=(
                f"the rule re-selected {nominal} in only {stability:.0%} of bootstrap "
                f"replicates, below the precommitted {SELECTION_STABILITY_MIN:.0%} — the "
                "point estimate is a selection artefact, not a bound"
            ),
            selection_frequency=frequency,
            optimism=optimism,
        )

    return rows, Decision(
        selected_arm=nominal,
        selection_frequency=frequency,
        optimism=optimism,
    )


def implied_call_ceiling(
    *, selected_bound: int, structural_tokens_p95: int, safety_factor: float = 1.2
) -> dict[str, Any]:
    """The call output ceiling the selected bound implies (AC-3).

    Derived from the token decomposition rather than from a ratio of successes. The
    rendered bound is what a reader pays; the call ceiling is what the provider bills,
    and the gap between them is envelope — braces, keys, basis tags. Conflating the two
    is the eight-fold mismatch that produces today's truncation.

    Args:
        selected_bound: The recommended rendered-token maximum.
        structural_tokens_p95: 95th percentile of measured structural overhead.
        safety_factor: Headroom above the p95, because a ceiling set at the observed
            maximum truncates the next call that exceeds it.

    Returns:
        The recommendation and the parts it was built from.
    """
    ceiling = round((selected_bound + structural_tokens_p95) * safety_factor)
    return {
        "rendered_bound": selected_bound,
        "structural_tokens_p95": structural_tokens_p95,
        "safety_factor": safety_factor,
        "recommended_call_ceiling": ceiling,
    }


# ── Delivery tables for the write-up ────────────────────────────────────────


#: Candidate rendered-token thresholds the reachability table is reported over.
DELIVERY_THRESHOLDS: tuple[int, ...] = (120, 180, 250, 300, 350, 400, 450, 500, 600)


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    return cov / ((vx * vy) ** 0.5) if vx and vy else 0.0


def _cv(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return (statistics.pstdev(values) / mean) if mean else 0.0


def delivery_tables(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Recompute every table the write-up quotes, from the run's own records.

    Exists so the published figures are reproducible rather than hand-derived. Three
    filter conventions are applied and **named in the output**, because they differ and
    a silent difference between them is how two correct computations disagree:

    * ``all`` — every call, including empties and parse failures.
    * ``content_bearing`` — calls that produced at least one item. Length and
      reachability are properties of a digest that exists.
    * ``paired`` — sessions where both arms of a comparison produced a length.

    Args:
        records: The run's generation records, as written to ``generations.jsonl``.

    Returns:
        Per-arm delivery, the reachability grid, the token decomposition, the paired
        arm comparisons, and the absolute-versus-relative shape statistics.
    """
    by_arm: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_arm.setdefault(str(r["arm"]), []).append(r)

    out: dict[str, Any] = {"arms": {}, "filters": {}}
    for arm, rows in by_arm.items():
        content = [r for r in rows if r.get("content_bearing") and r.get("rendered_tokens")]
        # Per-record order, NOT sorted: the shape statistics below pair each digest's
        # length with its own session's input size. Correlating a sorted length series
        # against an unsorted size series measures nothing at all.
        paired_len = [int(r["rendered_tokens"]) for r in content]
        paired_size = [float(r["prompt_tokens"]) for r in content if r.get("prompt_tokens")]
        rendered = sorted(paired_len)
        struct = sorted(
            int(r["structural_tokens"]) for r in content if r.get("structural_tokens") is not None
        )
        outs = sorted(int(r["completion_tokens"]) for r in content if r.get("completion_tokens"))
        ratios = [
            int(r["completion_tokens"]) / int(r["rendered_tokens"])
            for r in content
            if r.get("completion_tokens")
        ]
        rel = [
            int(r["rendered_tokens"]) / float(r["prompt_tokens"])
            for r in content
            if r.get("prompt_tokens")
        ]
        out["arms"][arm] = {
            "n_calls": len(rows),
            "n_content_bearing": len(content),
            "outcomes": {
                o: sum(1 for r in rows if r["outcome"] == o)
                for o in sorted({str(r["outcome"]) for r in rows})
            },
            "rendered": {
                "min": rendered[0] if rendered else None,
                "p50": _percentile(rendered, 0.5),
                "p90": _percentile(rendered, 0.9),
                "max": rendered[-1] if rendered else None,
                "mean": round(statistics.fmean(rendered), 1) if rendered else None,
            },
            "within": {
                t: round(sum(1 for x in rendered if x <= t) / len(rendered), 3)
                for t in DELIVERY_THRESHOLDS
            }
            if rendered
            else {},
            "tokens": {
                "output_p50": _percentile(outs, 0.5),
                "structural_p50": _percentile(struct, 0.5),
                "structural_p95": _percentile(struct, 0.95),
                "output_per_rendered_p50": round(statistics.median(ratios), 2) if ratios else None,
            },
            "shape": {
                "pearson_r": round(_pearson(paired_size, [float(x) for x in paired_len]), 2)
                if len(paired_size) == len(paired_len) and len(paired_len) > 2
                else None,
                "ratio_p50": round(statistics.median(rel), 3) if rel else None,
                "ratio_min": round(min(rel), 3) if rel else None,
                "ratio_max": round(max(rel), 3) if rel else None,
                "cv_absolute": round(_cv([float(x) for x in rendered]), 3) if rendered else None,
                "cv_relative": round(_cv(rel), 3) if rel else None,
            },
        }

    pooled_struct = sorted(
        int(r["structural_tokens"])
        for r in records
        if r.get("content_bearing") and r.get("structural_tokens") is not None
    )
    out["pooled"] = {
        "structural_p95_content_bearing": _percentile(pooled_struct, 0.95),
        "largest_billed_output": max(
            (int(r["completion_tokens"]) for r in records if r.get("completion_tokens")), default=0
        ),
        "truncated": sum(1 for r in records if r.get("truncated")),
        "n_calls": len(records),
    }
    out["filters"] = {
        "rendered_and_reachability": "content_bearing",
        "token_decomposition": "content_bearing",
        "shape": "content_bearing",
        "note": (
            "A digest that produced nothing has no length, so it is excluded from every "
            "length statistic. Outcome counts are over all calls."
        ),
    }

    lengths = {
        arm: {
            str(r["session_id"]): int(r["rendered_tokens"])
            for r in rows
            if r.get("content_bearing") and r.get("rendered_tokens")
        }
        for arm, rows in by_arm.items()
    }
    out["paired"] = {}
    for tighter, looser in (
        ("t120", "t250"),
        ("t120", "unbounded"),
        ("t250", "unbounded"),
        ("t180", "t250"),
    ):
        if tighter not in lengths or looser not in lengths:
            continue
        shared = sorted(set(lengths[tighter]) & set(lengths[looser]))
        deltas = [lengths[looser][s] - lengths[tighter][s] for s in shared]
        if deltas:
            out["paired"][f"{looser}_minus_{tighter}"] = {
                "n": len(deltas),
                "median": statistics.median(deltas),
                "mean": round(statistics.fmean(deltas), 1),
                "looser_longer_on": sum(1 for d in deltas if d > 0),
            }
    return out
