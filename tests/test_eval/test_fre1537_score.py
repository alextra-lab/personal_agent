"""FRE-1511 / ADR-0154 D7: the scorer checks every threshold and never pools (AC-2)."""

from __future__ import annotations

import copy
from collections.abc import Mapping

import pytest
from scripts.eval.fre1537 import score
from scripts.eval.fre1537.fixtures import load_fixtures

TRIALS = 3
GOOD_FINGERPRINT: dict[str, object] = {
    "engine": {"name": "llama.cpp", "build": "b6789-abcdef"},
    "model": {"name": "unsloth/qwen3.8-flash-next", "quant": "UD-IQ4_XS"},
    "planner_mode": {
        "name": "thinking_off",
        "params": {"chat_template_kwargs": {"enable_thinking": False}},
    },
    "system_prompt_sha256": "a" * 64,
}


def _plan(outcome: str) -> dict[str, object]:
    if outcome == "decline":
        return {"parse_error": False, "strategy": "SINGLE", "task_count": 0, "declined": True}
    if outcome == "expand":
        return {"parse_error": False, "strategy": "HYBRID", "task_count": 2, "declined": False}
    return {"parse_error": True, "raw": "not json"}


def make_decide_rows(wrong: Mapping[str, int] | None = None) -> list[dict[str, object]]:
    """Build 3 draws for every scored fixture. ``wrong`` flips the first N draws of a group.

    Groups: ``single_decline``, ``followup_decline``, ``single_expand``, ``followup_expand``.
    """
    wrong = dict(wrong or {})
    rows: list[dict[str, object]] = []
    for fx in load_fixtures():
        if fx.expected == "excluded":
            continue
        group = f"{fx.kind}_{fx.expected}"
        for trial in range(TRIALS):
            flip = wrong.get(group, 0) > 0
            if flip:
                wrong[group] -= 1
            got = ("expand" if fx.expected == "decline" else "decline") if flip else fx.expected
            rows.append(
                {
                    "arm": "decide",
                    "label": fx.label,
                    "trial": trial,
                    "expected": fx.expected,
                    "kind": fx.kind,
                    "secs": 0.8 if got == "decline" else 9.6,
                    "reasoning_chars": 0,
                    "usage": {"completion_tokens": 13 if got == "decline" else 319},
                    "plan": _plan(got),
                    "declined": got == "decline",
                }
            )
    return rows


def make_longhist_rows(cold: float = 40.2, extended: float = 5.9) -> list[dict[str, object]]:
    def call(secs: float) -> dict[str, object]:
        return {
            "secs": secs,
            "reasoning_chars": 0,
            "plan": _plan("decline"),
            "usage": {"completion_tokens": 13},
        }

    return [
        {
            "size_chars": 8000,
            "cold": call(2.0),
            "primary_between": {"secs": 1},
            "extended": call(0.9),
        },
        {
            "size_chars": 30000,
            "cold": call(15.0),
            "primary_between": {"secs": 1},
            "extended": call(2.5),
        },
        {
            "size_chars": 60000,
            "cold": call(cold),
            "primary_between": {"secs": 1},
            "extended": call(extended),
        },
    ]


def run(
    decide: list[dict[str, object]] | None = None,
    longhist: list[dict[str, object]] | None = None,
    fingerprint: Mapping[str, object] | None = None,
) -> score.Report:
    return score.score(
        decide_rows=make_decide_rows() if decide is None else decide,
        longhist_rows=make_longhist_rows() if longhist is None else longhist,
        timing_rows=[],
        fingerprint=GOOD_FINGERPRINT if fingerprint is None else fingerprint,
    )


def line(report: score.Report, name: str) -> score.Check:
    return next(c for c in report.checks if c.name == name)


def test_a_clean_run_passes_every_threshold() -> None:
    report = run()
    assert report.passed, report.render()
    assert (
        len(report.checks) == 11
    )  # 10 D7 thresholds (follow-ups split by direction) + fingerprint
    assert "RESULT: PASS" in report.render()


def test_scorer_fails_pooled_pass() -> None:
    """AC-2: 26/30 decline-correct with 48/48 expand-correct pools to 95%. It must fail."""
    report = run(decide=make_decide_rows({"single_decline": 4}))
    assert line(report, "Decline-correct").measured.startswith("26/30")
    assert line(report, "Expand-correct").measured.startswith("48/48")
    assert not line(report, "Decline-correct").passed
    assert line(report, "Expand-correct").passed
    assert not report.passed
    assert "RESULT: FAIL" in report.render()


def test_report_prints_no_pooled_figure() -> None:
    text = run(decide=make_decide_rows({"single_decline": 4})).render().lower()
    for banned in ("pooled", "agreement", "weighted", "overall accuracy"):
        assert banned not in text
    assert "74/78" not in text


@pytest.mark.parametrize(
    ("group", "wrong", "check", "passes"),
    [
        ("single_decline", 2, "Decline-correct", True),
        ("single_decline", 3, "Decline-correct", False),
        ("single_expand", 5, "Expand-correct", True),
        ("single_expand", 6, "Expand-correct", False),
        ("followup_decline", 1, "Follow-ups decline-correct", True),
        ("followup_decline", 2, "Follow-ups decline-correct", False),
        ("followup_expand", 1, "Follow-ups expand-correct", True),
        ("followup_expand", 2, "Follow-ups expand-correct", False),
    ],
)
def test_decision_thresholds_at_the_boundary(
    group: str, wrong: int, check: str, passes: bool
) -> None:
    report = run(decide=make_decide_rows({group: wrong}))
    assert line(report, check).passed is passes, report.render()


def test_decline_rate_counts_the_follow_up_draws_too() -> None:
    """Follow-up draws sit inside the 30 and the 48, so 3 wrong follow-up draws fail the 28/30."""
    report = run(decide=make_decide_rows({"followup_decline": 3}))
    assert line(report, "Decline-correct").measured.startswith("27/30")
    assert not line(report, "Decline-correct").passed


def test_a_parse_failure_fails_and_counts_as_incorrect() -> None:
    rows = make_decide_rows()
    rows[0]["plan"] = _plan("invalid")
    rows[0]["declined"] = None
    report = run(decide=rows)
    assert not line(report, "Plans that fail to parse or validate").passed
    assert line(report, "Plans that fail to parse or validate").measured == "1"
    assert line(report, "Decline-correct").measured.startswith("29/30")


def test_a_single_plan_with_tasks_is_invalid_not_an_expansion() -> None:
    rows = make_decide_rows()
    expand_row = next(r for r in rows if r["expected"] == "expand")
    expand_row["plan"] = {
        "parse_error": False,
        "strategy": "SINGLE",
        "task_count": 2,
        "declined": False,
    }
    report = run(decide=rows)
    assert line(report, "Expand-correct").measured.startswith("47/48")
    assert line(report, "Plans that fail to parse or validate").measured == "1"


def test_any_reasoning_character_fails() -> None:
    rows = make_decide_rows()
    rows[5]["reasoning_chars"] = 1
    assert not line(run(decide=rows), "Reasoning characters on any call").passed


def test_a_reasoning_character_in_the_long_history_arm_fails() -> None:
    long_rows = make_longhist_rows()
    long_rows[1]["cold"]["reasoning_chars"] = 40  # type: ignore[index]
    assert not line(run(longhist=long_rows), "Reasoning characters on any call").passed


@pytest.mark.parametrize(("tokens", "passes"), [(40, True), (41, False)])
def test_completion_tokens_p50_on_declined_calls(tokens: int, passes: bool) -> None:
    rows = make_decide_rows()
    for r in rows:
        if r["declined"]:
            r["usage"] = {"completion_tokens": tokens}
    assert line(run(decide=rows), "Completion tokens, declined calls (p50)").passed is passes


@pytest.mark.parametrize(("secs", "passes"), [(2.0, True), (2.1, False)])
def test_declined_single_turn_call_time_p50(secs: float, passes: bool) -> None:
    rows = make_decide_rows()
    for r in rows:
        if r["declined"] and r["kind"] == "single":
            r["secs"] = secs
    assert line(run(decide=rows), "Declined call time, single-turn (p50)").passed is passes


def test_follow_up_declines_do_not_count_toward_the_single_turn_time() -> None:
    rows = make_decide_rows()
    for r in rows:
        if r["declined"] and r["kind"] == "followup":
            r["secs"] = 30.0
    assert line(run(decide=rows), "Declined call time, single-turn (p50)").passed


@pytest.mark.parametrize(("extended", "passes"), [(10.0, True), (10.1, False)])
def test_long_history_extended_call(extended: float, passes: bool) -> None:
    assert (
        line(
            run(longhist=make_longhist_rows(extended=extended)),
            "Planner call, 60,000 chars, extended",
        ).passed
        is passes
    )


@pytest.mark.parametrize(("cold", "passes"), [(60.0, True), (60.1, False)])
def test_long_history_cold_call(cold: float, passes: bool) -> None:
    assert (
        line(run(longhist=make_longhist_rows(cold=cold)), "Planner call, 60,000 chars, cold").passed
        is passes
    )


def test_missing_long_history_arm_fails_both_delay_thresholds() -> None:
    report = run(longhist=[])
    assert not line(report, "Planner call, 60,000 chars, extended").passed
    assert not line(report, "Planner call, 60,000 chars, cold").passed


def test_a_short_sample_fails_instead_of_being_scaled() -> None:
    rows = [r for r in make_decide_rows() if r["trial"] != 2]
    report = run(decide=rows)
    assert not line(report, "Decline-correct").passed
    assert "need 30" in line(report, "Decline-correct").measured


def test_an_error_row_is_reported_and_leaves_the_sample_short() -> None:
    rows = make_decide_rows()
    rows[0] = {
        "arm": "decide",
        "label": rows[0]["label"],
        "trial": 0,
        "expected": rows[0]["expected"],
        "kind": rows[0]["kind"],
        "error": "ReadTimeout",
    }
    report = run(decide=rows)
    assert not report.passed
    assert "call errors: 1" in report.render()


def test_the_excluded_fixture_is_never_scored() -> None:
    rows = make_decide_rows()
    rows.append(
        {**copy.deepcopy(rows[0]), "label": "c5_noverb", "expected": "excluded", "kind": "single"}
    )
    assert line(run(decide=rows), "Decline-correct").measured.startswith("30/30")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f["engine"].pop("build"),
        lambda f: f["model"].update(quant="unknown"),
        lambda f: f.pop("system_prompt_sha256"),
        lambda f: f["planner_mode"].pop("params"),
    ],
)
def test_an_incomplete_fingerprint_fails_the_verdict(mutate: object) -> None:
    fingerprint = copy.deepcopy(GOOD_FINGERPRINT)
    mutate(fingerprint)  # type: ignore[operator]
    report = run(fingerprint=fingerprint)
    assert not report.passed
    assert not line(report, "Configuration fingerprint complete").passed


def test_the_report_carries_the_fingerprint() -> None:
    text = run().render()
    assert "b6789-abcdef" in text and "UD-IQ4_XS" in text and "a" * 64 in text


@pytest.mark.parametrize(
    ("k", "n", "text"),
    [
        (30, 30, "30/30 = 100% (89–100%)"),
        (46, 48, "46/48 = 96% (86–99%)"),
        (28, 30, "28/30 = 93% (79–98%)"),
    ],
)
def test_wilson_interval_matches_appendix_a(k: int, n: int, text: str) -> None:
    assert score.rate_text(k, n) == text


def test_percentile_uses_the_index_rule_of_appendix_a4() -> None:
    assert score.percentile([4.0, 1.0, 3.0, 2.0], 0.5) == 3.0  # index round(0.5 * 3) = 2
    assert score.percentile([], 0.5) is None
