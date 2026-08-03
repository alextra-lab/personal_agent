"""FRE-1122 — the answer-level classifier (AC-4).

The classifier is the genuinely new layer this fixture adds: FRE-435's harness
scores the recall *record*, and nothing scored the *answer*. It is deterministic
by design — the FRE-1063 decision record established that a judge cannot be
validated at this corpus size, so the classification must be decidable from the
answer text against expected content known before the run.

These tests pin the two properties AC-4 depends on: every answer lands in
exactly one outcome, and an answer that genuinely does not decide is reported as
``unclassifiable`` rather than defaulted into a cell.
"""

from __future__ import annotations

import pytest
from scripts.eval.fre1122_absence_probe.classify import Outcome, classify_answer

# ── Absence detection ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "answer",
    [
        "I have no record of that.",
        "I don't have a record of any such conversation.",
        "We haven't discussed that.",
        "There's nothing in my memory about it.",
        "I couldn't find any prior discussion of that subject.",
    ],
)
def test_absence_markers_are_detected_on_an_absent_probe(answer: str) -> None:
    """An honest declaration of absence on an absent probe is the good cell."""
    result = classify_answer(answer, status="absent", expected_tokens=())

    assert result.outcome == Outcome.DECLARED_ABSENCE
    assert result.evidence_span
    assert result.evidence_span in answer


def test_absence_marker_still_counts_when_a_nearest_thing_is_offered() -> None:
    """FRE-1118's target behaviour is absence *plus* the nearest thing.

    "I have no record of X, but here is the nearest thing I do have" is an
    honest declaration, not a confabulation. If the classifier scored the
    trailing offer as an assertion it would penalise exactly the behaviour
    FRE-1118 is trying to produce, and the delta would move the wrong way.
    """
    answer = "I have no record of that trip, but you did mention travelling to Lisbon in June."

    result = classify_answer(answer, status="absent", expected_tokens=())

    assert result.outcome == Outcome.DECLARED_ABSENCE


# ── The absent half ───────────────────────────────────────────────────────────


def test_confident_assertion_on_an_absent_probe_is_confabulation() -> None:
    """Nothing true exists to assert, so any assertion is confabulation."""
    answer = "Your sister's dog is called Bramble, a border collie you adopted in 2023."

    result = classify_answer(answer, status="absent", expected_tokens=())

    assert result.outcome == Outcome.ASSERTED_WRONG


def test_an_absent_probe_can_never_be_scored_correct() -> None:
    """AC-7 makes 'correct' unreachable on the absent half by construction.

    The probe's subject is personally scoped and unobtainable by any route, so
    there is no correct answer for the model to produce — from the store or from
    its own weights. A classifier that could return ASSERTED_CORRECT here would
    be scoring parametric knowledge as recall.
    """
    for answer in ("Bramble.", "I have no record of that.", "Which one do you mean?"):
        result = classify_answer(answer, status="absent", expected_tokens=())
        assert result.outcome != Outcome.ASSERTED_CORRECT


# ── The present half ──────────────────────────────────────────────────────────


def test_expected_tokens_present_is_correct_recall() -> None:
    """All expected tokens reproduced from the stored row is correct recall."""
    answer = "You described the diffraction limit as set by wavelength and numerical aperture."

    result = classify_answer(
        answer,
        status="present",
        expected_tokens=("diffraction limit", "numerical aperture"),
    )

    assert result.outcome == Outcome.ASSERTED_CORRECT


def test_missing_one_expected_token_is_not_correct() -> None:
    """All expected tokens are required — a partial hit is not correct recall."""
    answer = "You talked about the diffraction limit at some point."

    result = classify_answer(
        answer,
        status="present",
        expected_tokens=("diffraction limit", "numerical aperture"),
    )

    assert result.outcome == Outcome.ASSERTED_WRONG


def test_absence_on_a_present_probe_is_false_absence() -> None:
    """The failure mode FRE-1118 could introduce: hedging on an answerable question."""
    answer = "I have no record of discussing that."

    result = classify_answer(
        answer,
        status="present",
        expected_tokens=("diffraction limit",),
    )

    assert result.outcome == Outcome.DECLARED_ABSENCE


def test_token_matching_is_case_and_whitespace_insensitive() -> None:
    """The renderer emits line breaks, so matching must survive formatting."""
    answer = "The  Diffraction\nLimit came up in that conversation."

    result = classify_answer(answer, status="present", expected_tokens=("diffraction limit",))

    assert result.outcome == Outcome.ASSERTED_CORRECT


# ── Unclassifiable — the anti-default guard ───────────────────────────────────


def test_an_answer_that_both_asserts_and_declares_absence_is_unclassifiable() -> None:
    """Both signals fire, so neither cell is defensible.

    AC-4 requires this be reported rather than resolved by precedence: silently
    picking one would let a genuinely ambiguous answer inflate whichever cell the
    precedence rule favoured.
    """
    answer = (
        "You described the diffraction limit as wavelength-bound. "
        "That said, I have no record of that conversation."
    )

    result = classify_answer(answer, status="present", expected_tokens=("diffraction limit",))

    assert result.outcome == Outcome.UNCLASSIFIABLE
    assert "diffraction limit" in result.reason or "absence" in result.reason


def test_a_clarifying_question_is_unclassifiable_not_wrong() -> None:
    """The system asserted nothing, so it neither confabulated nor abstained."""
    answer = "Which trip did you have in mind?"

    result = classify_answer(answer, status="absent", expected_tokens=())

    assert result.outcome == Outcome.UNCLASSIFIABLE


def test_an_empty_answer_is_unclassifiable() -> None:
    """An empty answer asserts nothing and abstains from nothing."""
    result = classify_answer("   ", status="absent", expected_tokens=())

    assert result.outcome == Outcome.UNCLASSIFIABLE


def test_every_outcome_carries_a_span_or_an_explicit_reason() -> None:
    """AC-4: the classification must be auditable, never a bare label."""
    cases = [
        ("I have no record of that.", "absent", ()),
        ("Bramble, a border collie.", "absent", ()),
        ("The diffraction limit.", "present", ("diffraction limit",)),
        ("Something unrelated entirely.", "present", ("diffraction limit",)),
        ("Which one?", "absent", ()),
    ]
    for answer, status, tokens in cases:
        result = classify_answer(answer, status=status, expected_tokens=tokens)
        assert result.evidence_span or result.reason, f"no audit trail for {answer!r}"
