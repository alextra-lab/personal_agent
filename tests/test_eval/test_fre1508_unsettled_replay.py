"""FRE-1508 replay comparison — the pure core that decides AC-2 and AC-3."""

from __future__ import annotations

import pytest
from scripts.eval.fre1508_unsettled_replay import compare


def _row(
    index: int,
    replay: str,
    *,
    trace_id: str = "t1",
    captured: str | None = None,
    verdict: str | None = None,
    excluded: str | None = None,
) -> dict[str, object]:
    return {
        "trace_id": trace_id,
        "index": index,
        "captured_outcome": captured or replay,
        "replay_outcome": replay,
        "verdict": verdict,
        "excluded": excluded,
    }


def test_a_judged_unsettled_span_counts_as_a_fall_and_violates_nothing() -> None:
    """The case the fix exists for: a judge settles a span, and the count falls."""
    before = [_row(0, "unverifiable_by_containment"), _row(1, "not_contained")]
    after = [
        _row(0, "not_entailed", captured="unverifiable_by_containment", verdict="not_supported"),
        _row(1, "not_contained"),
    ]

    result = compare(before, after, min_turns=1)

    assert result.valid is True
    assert (result.unsettled_before, result.unsettled_after) == (1, 0)
    assert result.settled_changed == ()
    assert result.passed_without_verdict == ()
    assert (result.fidelity_matched, result.fidelity_total) == (2, 2)


def test_a_settled_span_that_moves_is_an_ac3_violation() -> None:
    """AC-3: any change to an outcome that was settled before the fix is reported."""
    before = [_row(0, "not_contained")]
    after = [_row(0, "passed", captured="not_contained")]

    assert len(compare(before, after, min_turns=1).settled_changed) == 1


def test_unsettled_to_passed_without_a_supported_verdict_is_an_ac2_violation() -> None:
    """AC-2: a span may not reach ``passed`` unless the judge said ``supported``."""
    before = [_row(0, "unverifiable_by_containment")]
    after = [_row(0, "passed", captured="unverifiable_by_containment")]

    assert len(compare(before, after, min_turns=1).passed_without_verdict) == 1


def test_excluded_spans_are_not_counted() -> None:
    """A span the replay could not rebuild must not move either count."""
    before = [_row(0, "unverifiable_by_containment", excluded="memory")]
    after = [_row(0, "passed", captured="unverifiable_by_containment", excluded="memory")]

    result = compare(before, after, min_turns=0)

    assert result.spans == 0
    assert result.changes == ()


def test_a_turn_the_replay_does_not_reproduce_is_dropped_whole() -> None:
    """A turn with one unfaithful span is not evidence for any of its spans."""
    before = [
        _row(0, "not_contained", trace_id="bad", captured="unverifiable_by_containment"),
        _row(1, "unverifiable_by_containment", trace_id="bad"),
        _row(0, "unverifiable_by_containment", trace_id="good"),
    ]
    after = [
        _row(0, "not_contained", trace_id="bad", captured="unverifiable_by_containment"),
        _row(1, "not_entailed", trace_id="bad", captured="unverifiable_by_containment"),
        _row(0, "unverifiable_by_containment", trace_id="good"),
    ]

    result = compare(before, after, min_turns=1)

    assert (result.turns, result.turns_dropped, result.spans) == (1, 1, 1)
    assert (result.fidelity_matched, result.fidelity_total) == (2, 3)
    assert result.changes == ()


def test_too_few_faithful_turns_make_the_run_invalid() -> None:
    """The bar recorded on the ticket: fewer than 10 turns is not a replay."""
    rows = [_row(0, "unverifiable_by_containment", trace_id=f"t{n}") for n in range(9)]

    result = compare(rows, rows)

    assert result.valid is False
    assert "9 turns" in result.invalid_reason


def test_runs_that_include_different_spans_are_invalid() -> None:
    """Both runs must have judged the same input, or the fall means nothing."""
    before = [_row(0, "unverifiable_by_containment"), _row(1, "unverifiable_by_containment")]
    after = [_row(0, "unverifiable_by_containment"), _row(1, "passed", excluded="memory")]

    result = compare(before, after, min_turns=1)

    assert result.valid is False
    assert "same spans" in result.invalid_reason


def test_a_repeated_span_key_is_refused() -> None:
    """A repeated key would silently drop a row from the comparison."""
    with pytest.raises(ValueError, match="duplicate"):
        compare([_row(0, "passed"), _row(0, "passed")], [])
