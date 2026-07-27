"""FRE-994 scoring gates, generation classification, and the decision rule.

These cover the parts that decide what the study is allowed to conclude: the validity
gates that can bar the loss endpoint from selecting a bound, the classifier that decides
whether a reply counts as delivered, and the rule that turns per-arm numbers into an
answer — including every path on which it must refuse to answer.
"""

# ruff: noqa: D103

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from scripts.eval.fre994_digest_compression_curve import analysis, arms, generate, scoring

from personal_agent.memory.session_digest import DigestItem, SessionDigest

_T0 = datetime(2026, 7, 27, tzinfo=timezone.utc)


# ── Validity gates ──────────────────────────────────────────────────────────


def test_extractor_recall_fails_below_the_precommitted_threshold() -> None:
    """The gate exists to be failable.

    A near miss is the case where relaxing is most tempting, so the threshold is a
    module constant and the comparison is a plain inequality — 41 of 52 is 0.788,
    and 0.788 is not 0.80.
    """
    result = scoring.extractor_recall(matched=41, reference_total=52)

    assert result.value == pytest.approx(0.788, abs=0.001)
    assert result.passed is False
    assert result.detail == {"matched": 41, "reference_total": 52}


def test_spurious_rate_is_an_upper_bound_not_a_floor() -> None:
    """Recall and spuriousness point in opposite directions, and a gate that compared both the same way would pass an extractor that invented freely."""
    assert scoring.extractor_spurious_rate(spurious=7, extracted_total=53).passed is True
    assert scoring.extractor_spurious_rate(spurious=30, extracted_total=53).passed is False


def test_kappa_is_reported_beside_raw_agreement_because_agreement_inflates() -> None:
    """A judge answering `covered` unconditionally scores high on raw agreement whenever `covered` dominates — which it does here.

    κ corrects for that chance agreement, so the two together catch a scorer that
    raw agreement alone would pass.
    """
    lazy = [("covered", "covered")] * 8 + [("missing", "covered"), ("partial", "covered")]

    agreement, kappa = scoring.judge_agreement(lazy)

    assert agreement.value == pytest.approx(0.8)
    assert agreement.passed is True  # raw agreement alone would let this through
    assert kappa.value < scoring.JUDGE_KAPPA_MIN
    assert kappa.passed is False


def test_kappa_does_not_report_perfect_agreement_as_zero() -> None:
    """With one label used by both raters there is no chance-agreement baseline, so κ is undefined.

    Reporting the degenerate case as 1.0 with a flag is honest; reporting it as 0.0
    would fail a scorer that never disagreed.
    """
    _, kappa = scoring.judge_agreement([("covered", "covered")] * 10)

    assert kappa.value == 1.0
    assert kappa.detail["degenerate_single_label"] is True


def test_anchors_are_directional() -> None:
    """An empty digest must score near zero and a reference against itself near one; the two thresholds are compared in opposite directions and swapping them would pass a broken scorer."""
    good = scoring.anchor_results(empty_retention=0.0, self_retention=1.0)
    bad = scoring.anchor_results(empty_retention=0.9, self_retention=0.1)

    assert all(a.passed for a in good)
    assert not any(a.passed for a in bad)


def test_empty_digest_renders_as_a_marker_not_as_nothing() -> None:
    """A judge handed an empty string cannot tell an empty digest from a failed render, and would score the two the same."""
    rendered = scoring.render_digest(SessionDigest())

    assert "empty" in rendered.lower()


def test_the_definition_is_shared_by_the_reference_the_extractor_and_the_judge() -> None:
    """Three components applying three private notions of "consequential" would measure three different things and report one number."""
    assert scoring.CONSEQUENTIAL_DEFINITION in scoring._EXTRACTION_SYSTEM
    assert scoring.CONSEQUENTIAL_DEFINITION in scoring._JUDGE_SYSTEM


def test_partial_coverage_does_not_count_as_carried() -> None:
    """An item half-carried is an item a future reader cannot rely on, so `partial` scores 0 in the primary endpoint — precommitted, with the lenient variant kept only for the reported sensitivity analysis."""
    assert "partial" not in scoring.COVERED_VERDICTS
    assert scoring.COVERED_VERDICTS_LENIENT["partial"] == 0.5


# ── Generation classification ───────────────────────────────────────────────


def _response(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tool_calls": [],
        "content": "",
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        "cost_usd": 0.01,
    }
    base.update(over)
    return base


def _tool_reply(payload: str) -> list[dict[str, object]]:
    from personal_agent.memory.session_digest_wire import DIGEST_TOOL_NAME

    return [{"name": DIGEST_TOOL_NAME, "arguments": payload}]


def test_an_empty_reply_at_the_ceiling_is_truncation_not_silence() -> None:
    """A generation that exhausted its budget before emitting anything usable is a truncation.

    Testing `empty` first mislabels it and understates the truncation rate — the
    error FRE-996 made on its own first pass and had to reclassify.
    """
    arm = arms.ARMS_BY_NAME["t250"]
    at_ceiling = _response(
        finish_reason="length", usage={"prompt_tokens": 100, "completion_tokens": arm.call_ceiling}
    )

    record = generate.classify(at_ceiling, arm=arm, session_id="s", ended_at=_T0)

    assert record.outcome == "truncated"
    assert record.truncated is True


def test_an_empty_reply_below_the_ceiling_is_empty() -> None:
    arm = arms.ARMS_BY_NAME["t250"]

    record = generate.classify(_response(), arm=arm, session_id="s", ended_at=_T0)

    assert record.outcome == "empty"
    assert record.truncated is False


def test_a_digest_that_parses_but_fills_no_slot_is_not_delivered() -> None:
    """ADR-0124 allows an empty digest, but in production it returns GENERATED and marks the session clean forever.

    It is the failure most hostile to the consumer, so it is counted, not
    absorbed into `ok`.
    """
    arm = arms.ARMS_BY_NAME["t250"]
    payload = '{"label": "Nothing settled", "digest": {"established": [], "decisions": [], '
    payload += '"unresolved": [], "corrections": []}}'

    record = generate.classify(
        _response(tool_calls=_tool_reply(payload)), arm=arm, session_id="s", ended_at=_T0
    )

    assert record.outcome == "empty"
    assert record.content_bearing is False


def test_a_valid_digest_records_its_delivery_measurements() -> None:
    arm = arms.ARMS_BY_NAME["t250"]
    payload = (
        '{"label": "Shard triage", "digest": {"established": '
        '[{"text": "The cluster is green", "basis": "assistant_reasoning"}], '
        '"decisions": [], "unresolved": [], "corrections": []}}'
    )

    record = generate.classify(
        _response(tool_calls=_tool_reply(payload)), arm=arm, session_id="s", ended_at=_T0
    )

    assert record.outcome == "ok"
    assert record.content_bearing is True
    assert record.rendered_tokens is not None
    assert record.within_bound is True
    assert record.content_tokens is not None


def test_a_parsed_digest_at_the_ceiling_gets_its_own_class() -> None:
    """A digest cut off mid-list still parses as a valid, shorter digest.

    Scoring that as clean is the cheapest way this measurement produces a false
    success.
    """
    arm = arms.ARMS_BY_NAME["t250"]
    payload = (
        '{"label": "Shard triage", "digest": {"established": '
        '[{"text": "The cluster is green", "basis": "assistant_reasoning"}], '
        '"decisions": [], "unresolved": [], "corrections": []}}'
    )

    record = generate.classify(
        _response(
            tool_calls=_tool_reply(payload),
            finish_reason="length",
            usage={"prompt_tokens": 100, "completion_tokens": arm.call_ceiling},
        ),
        arm=arm,
        session_id="s",
        ended_at=_T0,
    )

    assert record.outcome == "ok_at_ceiling"


def test_the_unbounded_arm_cannot_fall_outside_a_bound_it_does_not_have() -> None:
    """`within_bound` on the reference arm must not be computed against `max_tokens`, which is zero there — every digest would score as over budget and the reachability column would be nonsense."""
    arm = arms.ARMS_BY_NAME["unbounded"]
    payload = (
        '{"label": "Long one", "digest": {"established": '
        '[{"text": "' + ("x " * 400) + '", "basis": "assistant_reasoning"}], '
        '"decisions": [], "unresolved": [], "corrections": []}}'
    )

    record = generate.classify(
        _response(tool_calls=_tool_reply(payload)), arm=arm, session_id="s", ended_at=_T0
    )

    assert record.within_bound is True


# ── Decision rule ───────────────────────────────────────────────────────────


def _outcome(
    sid: str, arm: str, *, lost: bool | None, tokens: int = 100
) -> analysis.SessionOutcome:
    return analysis.SessionOutcome(
        session_id=sid,
        arm=arm,
        lost_a_conclusion=lost,
        rendered_tokens=tokens,
        within_bound=True,
        content_bearing=True,
        truncated=False,
    )


def _sessions(n: int) -> list[str]:
    return [f"s{i}" for i in range(n)]


def test_no_loss_verdict_reports_the_instrument_not_the_digest() -> None:
    """A missing loss verdict is a finding about the instrument, not about the digest.

    Reporting the first when the second happened would claim evidence the run
    does not have — and after a failed §4.3 gate, that is exactly the mistake
    available.
    """
    by_arm = {
        arm: [_outcome(s, arm, lost=None) for s in _sessions(10)]
        for arm in ("t120", "t180", "t250", "unbounded")
    }

    _, decision = analysis.decide(
        by_arm,
        order=["t120", "t180", "t250"],
        reference_arm="unbounded",
        bounds={"t120": 120, "t180": 180, "t250": 250, "unbounded": None},
        n_sessions=10,
        replicates=50,
    )

    assert decision.selected_arm is None
    assert "never evaluated" in decision.inconclusive_reason


def test_a_non_monotone_curve_refuses_to_select_the_tighter_arm() -> None:
    """A tighter bound losing less than a looser one is not a compression curve, it is noise — and the rule as stated would reward that noise with the smallest bound."""
    sessions = _sessions(20)
    by_arm = {
        # t120 loses nothing while t180 loses half: the ordering is inverted.
        "t120": [_outcome(s, "t120", lost=False) for s in sessions],
        "t180": [_outcome(s, "t180", lost=i < 10) for i, s in enumerate(sessions)],
        "t250": [_outcome(s, "t250", lost=False) for s in sessions],
        "unbounded": [_outcome(s, "unbounded", lost=False) for s in sessions],
    }

    _, decision = analysis.decide(
        by_arm,
        order=["t120", "t180", "t250"],
        reference_arm="unbounded",
        bounds={"t120": 120, "t180": 180, "t250": 250, "unbounded": None},
        n_sessions=20,
        replicates=50,
    )

    assert decision.selected_arm is None
    assert ("t120", "t180") in decision.monotonicity_violations


def test_an_unreachable_bound_is_rejected_however_little_it_loses() -> None:
    """A bound the generator cannot hit is not a bound, it is a rejection rate.

    Reachability is a gate rather than a tiebreak precisely because the incumbent
    250 would have failed it — the contract's rendered p90 is 341-389.
    """
    sessions = _sessions(20)
    unreachable = [
        analysis.SessionOutcome(
            session_id=s,
            arm="t120",
            lost_a_conclusion=False,
            rendered_tokens=300,
            within_bound=False,
            content_bearing=True,
            truncated=False,
        )
        for s in sessions
    ]
    by_arm = {
        "t120": unreachable,
        "t250": [_outcome(s, "t250", lost=False) for s in sessions],
        "unbounded": [_outcome(s, "unbounded", lost=False) for s in sessions],
    }

    rows, decision = analysis.decide(
        by_arm,
        order=["t120", "t250"],
        reference_arm="unbounded",
        bounds={"t120": 120, "t250": 250, "unbounded": None},
        n_sessions=20,
        replicates=200,
    )

    assert {r.arm: r.reachability for r in rows}["t120"] == 0.0
    assert decision.selected_arm == "t250"


def test_the_paired_bootstrap_resamples_sessions_so_the_pairing_survives() -> None:
    """Every arm runs on the same sessions, so ΔL is a within-session difference.

    Resampling cells would let a session enter one arm's replicate and not the
    other's, turning a paired difference into an unpaired one and widening the
    interval for the wrong reason. When both arms agree on every session the
    difference is exactly zero, and a paired interval must show that.
    """
    sessions = _sessions(20)
    identical = [_outcome(s, "t250", lost=i % 3 == 0) for i, s in enumerate(sessions)]
    reference = [_outcome(s, "unbounded", lost=i % 3 == 0) for i, s in enumerate(sessions)]

    low, high = analysis.paired_bootstrap_ci(identical, reference, replicates=500)

    assert low == 0.0
    assert high == 0.0


def test_the_bootstrap_interval_is_reproducible_from_the_published_seed() -> None:
    """An interval nobody can recompute is not evidence."""
    sessions = _sessions(20)
    outcomes = [_outcome(s, "t250", lost=i % 4 == 0) for i, s in enumerate(sessions)]
    reference = [_outcome(s, "unbounded", lost=i % 7 == 0) for i, s in enumerate(sessions)]

    first = analysis.paired_bootstrap_ci(outcomes, reference, replicates=500)
    again = analysis.paired_bootstrap_ci(outcomes, reference, replicates=500)

    assert first == again


def test_an_unstable_selection_is_reported_as_inconclusive() -> None:
    """The rule takes a minimum over arms on a small sample, which is optimistic by construction.

    An arm that only wins because of which sessions happened to be drawn must not
    be published as a bound.
    """
    sessions = _sessions(20)
    # t120 sits exactly on the threshold, so resampling flips it in and out.
    by_arm = {
        "t120": [_outcome(s, "t120", lost=i < 2) for i, s in enumerate(sessions)],
        "t250": [_outcome(s, "t250", lost=i < 2) for i, s in enumerate(sessions)],
        "unbounded": [_outcome(s, "unbounded", lost=False) for s in sessions],
    }

    _, decision = analysis.decide(
        by_arm,
        order=["t120", "t250"],
        reference_arm="unbounded",
        bounds={"t120": 120, "t250": 250, "unbounded": None},
        n_sessions=20,
        replicates=2_000,
    )

    if decision.selected_arm is None:
        assert "replicates" in decision.inconclusive_reason
    else:
        assert decision.selection_frequency[decision.selected_arm] >= (
            analysis.SELECTION_STABILITY_MIN
        )


def test_delta_loss_is_measured_against_the_unbounded_arm_not_against_zero() -> None:
    """The generator omits things even unbounded, so an absolute loss rate confuses "the bound is too tight" with "the generator is imperfect"."""
    sessions = _sessions(20)
    # Both arms lose on the same 10 sessions: the bound added nothing.
    outcomes = [_outcome(s, "t250", lost=i < 10) for i, s in enumerate(sessions)]
    reference = [_outcome(s, "unbounded", lost=i < 10) for i, s in enumerate(sessions)]

    row = analysis.summarise_arm(outcomes, reference=reference, bound=250)

    assert row.loss_rate == pytest.approx(0.5)
    assert row.delta_loss == pytest.approx(0.0)


def test_the_implied_call_ceiling_comes_from_the_token_split_not_a_ratio() -> None:
    """AC-3's ceiling has to carry the envelope the provider bills, which a ratio of successes cannot supply."""
    result = analysis.implied_call_ceiling(selected_bound=250, structural_tokens_p95=500)

    assert result["recommended_call_ceiling"] == round((250 + 500) * 1.2)
    assert result["rendered_bound"] == 250


def test_empty_truncated_and_unusable_are_three_different_failures() -> None:
    """All three mean the session got no memory, and they are not interchangeable.

    A truncation and a parse failure leave the session dirty and retryable. An
    empty digest returns GENERATED, marks the session clean and is never retried
    — it is the failure that hides, and it is the one the sweep is meant to gate
    on. Counting "not content-bearing" as empty folds the loud classes into the
    silent one and overstates it.
    """
    sessions = _sessions(4)
    outcomes = [
        analysis.SessionOutcome(
            session_id=sessions[0],
            arm="t250",
            lost_a_conclusion=None,
            rendered_tokens=None,
            within_bound=False,
            content_bearing=False,
            truncated=True,
            outcome="truncated",
        ),
        analysis.SessionOutcome(
            session_id=sessions[1],
            arm="t250",
            lost_a_conclusion=None,
            rendered_tokens=None,
            within_bound=False,
            content_bearing=False,
            truncated=False,
            outcome="empty",
        ),
        analysis.SessionOutcome(
            session_id=sessions[2],
            arm="t250",
            lost_a_conclusion=None,
            rendered_tokens=None,
            within_bound=False,
            content_bearing=False,
            truncated=False,
            outcome="contract_drift",
        ),
        _outcome(sessions[3], "t250", lost=None),
    ]

    row = analysis.summarise_arm(outcomes, reference=[], bound=250)

    assert row.truncation_rate == pytest.approx(0.25)
    assert row.empty_rate == pytest.approx(0.25)
    assert row.unusable_rate == pytest.approx(0.25)
    assert row.content_bearing_rate == pytest.approx(0.25)


def test_an_arm_with_no_content_at_all_fails_reachability_closed() -> None:
    """Returning 1.0 when there is nothing to measure conflates "no bound to exceed" with "produced nothing".

    The second must fail: an arm whose every digest was empty has not
    demonstrated it can hit its bound, and failing open would let the rule
    recommend it.
    """
    empty = [
        analysis.SessionOutcome(
            session_id=s,
            arm="t120",
            lost_a_conclusion=False,
            rendered_tokens=None,
            within_bound=False,
            content_bearing=False,
            truncated=False,
            outcome="empty",
        )
        for s in _sessions(20)
    ]

    assert analysis.summarise_arm(empty, reference=[], bound=120).reachability == 0.0
    # An arm with no bound is still trivially reachable.
    assert analysis.summarise_arm(empty, reference=[], bound=None).reachability == 1.0


def test_delta_loss_is_paired_on_both_sides() -> None:
    """The point estimate the gate reads and the interval that describes it must be the same estimand.

    Cells drop out per-arm — a digest that failed to parse carries no verdict —
    so differencing each arm's own rate over its own sessions is two unpaired
    proportions subtracted, and the estimate can land outside its own interval.
    """
    sessions = _sessions(20)
    # The reference loses on session 0; the arm is unjudged there.
    outcomes = [_outcome(s, "t250", lost=False) for s in sessions[1:]]
    reference = [_outcome(s, "unbounded", lost=(i == 0)) for i, s in enumerate(sessions)]

    row = analysis.summarise_arm(outcomes, reference=reference, bound=250)

    # Over the 19 shared sessions neither loses anything, so ΔL is exactly zero.
    # An unpaired difference would report 0 - 1/20 = -0.05.
    assert row.delta_loss == pytest.approx(0.0)
    assert row.delta_loss_ci == (0.0, 0.0)


def test_a_missing_reference_arm_is_reported_as_such_not_as_absolute_loss() -> None:
    """Without the unbounded arm, ΔL silently becomes an absolute loss rate — which confuses "the bound is too tight" with "the generator is imperfect"."""
    sessions = _sessions(20)
    by_arm = {
        "t250": [_outcome(s, "t250", lost=i < 6) for i, s in enumerate(sessions)],
        "unbounded": [_outcome(s, "unbounded", lost=None) for s in sessions],
    }

    _, decision = analysis.decide(
        by_arm,
        order=["t250"],
        reference_arm="unbounded",
        bounds={"t250": 250, "unbounded": None},
        n_sessions=20,
        replicates=50,
    )

    assert decision.selected_arm is None
    assert "reference arm" in decision.inconclusive_reason


def test_an_unjudged_arm_does_not_manufacture_a_non_monotone_curve() -> None:
    """An unjudged arm's loss rate defaults to zero, which reads as "loses nothing" and inverts the ordering against every judged arm.

    That would report a finding about the digest when the truth is a missing
    measurement.
    """
    sessions = _sessions(20)
    by_arm = {
        "t120": [_outcome(s, "t120", lost=None) for s in sessions],  # never judged
        "t250": [_outcome(s, "t250", lost=i < 6) for i, s in enumerate(sessions)],
        "unbounded": [_outcome(s, "unbounded", lost=i < 4) for i, s in enumerate(sessions)],
    }

    _, decision = analysis.decide(
        by_arm,
        order=["t120", "t250"],
        reference_arm="unbounded",
        bounds={"t120": 120, "t250": 250, "unbounded": None},
        n_sessions=20,
        replicates=200,
    )

    assert decision.monotonicity_violations == []


def test_bootstrap_replicates_apply_the_monotonicity_precondition_too() -> None:
    """§6.3 requires the FULL rule per replicate, including §6.2.

    Applying only the selection step lets a replicate whose curve inverted still
    return its tightest passing arm, inflating that arm's re-selection share with
    exactly the noise §6.2 exists to suppress. A replicate that inverts must
    contribute no pick, so the frequencies cannot sum to one.
    """
    sessions = _sessions(20)
    # Rates close enough that resampling flips the ordering in a meaningful share.
    by_arm = {
        "t120": [_outcome(s, "t120", lost=i < 4) for i, s in enumerate(sessions)],
        "t180": [_outcome(s, "t180", lost=i < 3) for i, s in enumerate(sessions)],
        "t250": [_outcome(s, "t250", lost=i < 3) for i, s in enumerate(sessions)],
        "unbounded": [_outcome(s, "unbounded", lost=i < 2) for i, s in enumerate(sessions)],
    }

    _, decision = analysis.decide(
        by_arm,
        order=["t120", "t180", "t250"],
        reference_arm="unbounded",
        bounds={"t120": 120, "t180": 180, "t250": 250, "unbounded": None},
        n_sessions=20,
        replicates=2_000,
    )

    assert sum(decision.selection_frequency.values()) < 1.0, (
        "some replicates must return no pick — either inconclusive by §6.2 or by §6"
    )


def test_rendered_digest_carries_only_what_a_reader_would_see() -> None:
    """Judging the wire payload would let key names and basis tags stand in for content the reader never reads."""
    digest = SessionDigest(
        established=[DigestItem(text="The cluster is green", basis="assistant_reasoning")]
    )

    rendered = scoring.render_digest(digest)

    assert "The cluster is green" in rendered
    assert "assistant_reasoning" not in rendered
