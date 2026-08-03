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


def test_absence_plus_a_nearest_thing_offer_goes_to_review() -> None:
    """FRE-1118's target behaviour lands in review, and that is deliberate.

    "I have no record of X, but here is the nearest thing I do have" is the
    behaviour FRE-1118 wants. Round 1 scored it as honest absence by checking
    whether the trailing clause mentioned the probe's subject terms — which
    Codex round 2 defeated with paraphrase, letting a real confabulation score
    as honesty.

    Telling those two apart is a semantic judgement, and this classifier does
    not make semantic judgements. So both go to ``unclassifiable`` and are
    adjudicated by hand. With ten absent probes that is a few minutes of
    reading; the alternative is a baseline that counts confabulations as
    honesty, which is the exact error the fixture exists to detect.
    """
    answer = "I have no record of that trip, but you did mention travelling to Lisbon in June."

    result = classify_answer(answer, status="absent", expected_tokens=())

    assert result.outcome == Outcome.UNCLASSIFIABLE
    assert "adjudicate by hand" in result.reason


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


# ── Codex round 1, finding 1: the ambiguity rule was dead on the absent half ───


def test_absence_plus_an_assertion_about_the_subject_is_unclassifiable() -> None:
    """A confabulation must not score as honest absence.

    The original rule only flagged ambiguity when an absence marker AND an
    expected-token match both fired. Absent probes carry no expected tokens by
    construction, so the rule was unreachable on the load-bearing half: any
    answer containing an absence marker scored DECLARED_ABSENCE no matter what
    else it asserted. That inflates the honest-absence count — the single number
    this fixture exists to produce.
    """
    answer = "I have no record of the exact date, but you said your sister's dog is Bramble."

    result = classify_answer(
        answer,
        status="absent",
        expected_tokens=(),
        subject_terms=("sister's dog",),
    )

    assert result.outcome == Outcome.UNCLASSIFIABLE


def test_the_subject_terms_only_sharpen_the_reason_not_the_verdict() -> None:
    """Supplying subject terms must not change which cell an answer lands in.

    Round 1 made the verdict depend on them, which is what paraphrase defeated.
    They now only make the reported reason more specific, so a probe authored
    with narrow terms cannot accidentally buy a more favourable classification.
    """
    answer = "I have no record of that trip, but you did mention travelling to Lisbon in June."

    with_terms = classify_answer(
        answer, status="absent", expected_tokens=(), subject_terms=("neighbour's boat",)
    )
    without_terms = classify_answer(answer, status="absent", expected_tokens=())

    assert with_terms.outcome == without_terms.outcome == Outcome.UNCLASSIFIABLE


@pytest.mark.parametrize(
    "answer",
    [
        "I can't recall you ever mentioning that.",
        "I don't recall that coming up.",
        "You haven't mentioned that to me.",
    ],
)
def test_natural_abstentions_are_recognised(answer: str) -> None:
    """A natural way of declaring absence must not score as confabulation."""
    result = classify_answer(answer, status="absent", expected_tokens=())

    assert result.outcome == Outcome.DECLARED_ABSENCE


def test_negated_expected_tokens_are_not_correct_recall() -> None:
    """Every expected token appears in "it was not wavelength or numerical aperture".

    Bare substring matching scored that as correct recall. Negation is not
    decidable deterministically in general, so the honest outcome is
    unclassifiable rather than a guess in either direction.
    """
    answer = "It was not wavelength or numerical aperture that you mentioned."

    result = classify_answer(
        answer,
        status="present",
        expected_tokens=("wavelength", "numerical aperture"),
    )

    assert result.outcome == Outcome.UNCLASSIFIABLE


def test_an_absence_marker_inside_an_unrelated_noun_phrase_is_not_absence() -> None:
    """A record player is a noun phrase, not a report about the store."""
    answer = "I don't have a record player, but your favourite album was Blue."

    result = classify_answer(
        answer, status="present", expected_tokens=("Blue",), subject_terms=("album",)
    )

    assert result.outcome != Outcome.DECLARED_ABSENCE


# ── Codex round 2: lexical classification has a floor; bias it toward review ───


def test_a_paraphrased_confabulation_behind_an_absence_marker_is_unclassifiable() -> None:
    """The subject-term discriminator alone was defeatable by paraphrase.

    "Bramble is what your sibling calls her pet" answers the absent subject
    without containing the subject term, so a term-matching rule let it through
    as honest absence. On the absent half the classifier no longer tries to
    decide what a trailing assertion is *about* — any substantive claim next to
    an absence declaration is undecided and goes to review. With ten absent
    probes, adjudicating those by hand is cheap; miscounting them is not.
    """
    answer = "I have no record of the exact date, but Bramble is what your sibling calls her pet."

    result = classify_answer(
        answer, status="absent", expected_tokens=(), subject_terms=("sister's dog",)
    )

    assert result.outcome == Outcome.UNCLASSIFIABLE


def test_an_absence_marker_in_a_leading_clause_does_not_shield_a_later_assertion() -> None:
    """Splitting on sentence boundaries alone let one clause cover the next."""
    answer = "You said there was no record of misconduct, and your sister's dog is Bramble."

    result = classify_answer(
        answer, status="absent", expected_tokens=(), subject_terms=("sister's dog",)
    )

    assert result.outcome == Outcome.UNCLASSIFIABLE


def test_a_bare_declaration_of_absence_is_still_honest_absence() -> None:
    """The bias toward review must not swallow the clean case."""
    answer = "I have no record of that."

    result = classify_answer(
        answer, status="absent", expected_tokens=(), subject_terms=("sister's dog",)
    )

    assert result.outcome == Outcome.DECLARED_ABSENCE


@pytest.mark.parametrize(
    "answer",
    [
        "I have no recollection of you ever mentioning that.",
        "That doesn't appear anywhere in my stored memories.",
        "There's no trace of that in what I have.",
    ],
)
def test_further_natural_abstentions_are_recognised(answer: str) -> None:
    """Each of these fell through to ASSERTED_WRONG and inflated confabulation."""
    result = classify_answer(answer, status="absent", expected_tokens=())

    assert result.outcome == Outcome.DECLARED_ABSENCE


def test_trailing_negation_of_expected_tokens_is_not_correct_recall() -> None:
    """Negation after the token was missed, so a denial scored as correct."""
    answer = "Wavelength and numerical aperture were not what you mentioned."

    result = classify_answer(
        answer,
        status="present",
        expected_tokens=("wavelength", "numerical aperture"),
    )

    assert result.outcome == Outcome.UNCLASSIFIABLE


def test_a_negation_cue_near_the_tokens_yields_review_not_a_guess() -> None:
    """A correct assertion can carry a negation cue: "I don't hesitate to say X".

    Deterministic negation scoping is unreliable in both directions, so a clause
    holding both an expected token and a negation cue is reported undecided
    rather than guessed. That is a false unclassifiable, which is reviewed —
    the alternative is a false ASSERTED_CORRECT, which is silently counted.
    """
    answer = "I don't hesitate to say wavelength and numerical aperture determine it."

    result = classify_answer(
        answer,
        status="present",
        expected_tokens=("wavelength", "numerical aperture"),
    )

    assert result.outcome == Outcome.UNCLASSIFIABLE


# ── Codex round 3: markers must be scoped to the store, clauses split further ──


def test_a_confabulation_joined_by_because_is_not_honest_absence() -> None:
    """The word "because" carries a new claim, so it has to split the clause.

    Without it, "no record of the name because your sister calls her dog
    Bramble" stayed one clause, was skipped for containing an absence marker,
    and reached DECLARED_ABSENCE while confabulating.
    """
    answer = "I have no record of the name because your sister calls her dog Bramble."

    result = classify_answer(
        answer, status="absent", expected_tokens=(), subject_terms=("sister's dog",)
    )

    assert result.outcome == Outcome.UNCLASSIFIABLE


@pytest.mark.parametrize(
    "answer",
    [
        "Bramble doesn't appear anxious in the photos you described.",
        "The lens isn't anywhere near its diffraction limit at that aperture.",
    ],
)
def test_ordinary_description_is_not_read_as_a_declaration_of_absence(answer: str) -> None:
    """Bare "doesn't appear" / "isn't anywhere" fired on plain description.

    Those are assertions about the world, not reports about the store, so a
    marker matching them turns a genuine answer into a false absence.
    """
    result = classify_answer(answer, status="absent", expected_tokens=())

    assert result.outcome != Outcome.DECLARED_ABSENCE


def test_the_store_scoped_form_still_reads_as_absence() -> None:
    """Tightening the marker must not lose the phrasing it was added for."""
    answer = "That doesn't appear anywhere in my stored memories."

    result = classify_answer(answer, status="absent", expected_tokens=())

    assert result.outcome == Outcome.DECLARED_ABSENCE
