"""FRE-1122 — deterministic three-way classification of a rendered answer (AC-4).

This module is the layer FRE-435's harness does not have. That harness scores the
recall *record* — recall@k, precision@k, MRR — and the FRE-1116 analysis found
that the record does not distinguish the three outcomes the owner actually
experiences: a correct answer, a confabulation assembled from nearest
neighbours, and an honest report that nothing is stored.

**No judge, deliberately.** The FRE-1063 decision record established that an LLM
judge cannot be validated at this corpus size — a calibration set large enough to
trust one would be larger than the population being measured. Classification is
therefore decidable from the answer text against expected content that is known
*before* the run, which is what makes the probe set's construction-time ground
truth load-bearing rather than decorative.

The design constraint that shapes everything here: **an answer that does not
decide must be reported as undecided.** Resolving genuine ambiguity by
precedence would let it inflate whichever cell the precedence rule happened to
favour, and the single number this fixture produces would be quietly wrong.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

__all__ = ["Classification", "Outcome", "ProbeStatus", "classify_answer"]

ProbeStatus = Literal["present", "absent"]


class Outcome(enum.StrEnum):
    """The three outcomes, plus the explicit refusal to guess between them."""

    ASSERTED_CORRECT = "asserted_correct"
    ASSERTED_WRONG = "asserted_wrong"
    DECLARED_ABSENCE = "declared_absence"
    UNCLASSIFIABLE = "unclassifiable"


@dataclass(frozen=True)
class Classification:
    """One answer's outcome with the evidence that decided it.

    Attributes:
        outcome: The cell this answer lands in.
        evidence_span: The verbatim span of the answer that determined the
            outcome, quoted from the answer unmodified. Empty when the outcome
            was decided by the *absence* of a signal rather than its presence,
            in which case ``reason`` carries the audit trail.
        reason: Why this outcome, in one line. Always populated.
    """

    outcome: Outcome
    evidence_span: str
    reason: str


# Phrases by which the system reports it holds no record. Curated rather than
# inferred: each is a form observed in real captures or in the FRE-435 bespoke
# probe set's correct-abstention controls. A phrase is added here only when it
# unambiguously reports absence *of a stored record* — "I'm not sure" is a
# hedge about confidence, not a declaration of absence, and is deliberately out.
_ABSENCE_MARKERS: tuple[str, ...] = (
    "no record of",
    "no records of",
    # "record" must be followed by "of/that/any" — "I don't have a record player"
    # is a noun phrase, not a declaration of absence (Codex round 1, non-blocking 1).
    r"do(?:n't| not) have (?:a|any) record (?:of|that|for)",
    "no memory of",
    r"do(?:n't| not) have any memory",
    "nothing in my memory",
    "not in my memory",
    "no stored",
    "nothing stored",
    r"have ?n't discussed",
    "have not discussed",
    r"have ?n't talked about",
    "have not talked about",
    r"have ?n't mentioned",
    "have not mentioned",
    "no prior discussion",
    "no previous discussion",
    r"do(?:n't| not) have information about",
    r"do(?:n't| not) have anything about",
    r"could ?n't find any",
    "could not find any",
    r"did ?n't find any",
    "did not find any",
    "no conversation",
    "nothing on record",
    # Natural abstentions the first draft missed entirely, so they scored as
    # confabulation (Codex round 1, finding 1).
    r"can ?n?'?t recall",
    "cannot recall",
    r"do(?:n't| not) recall",
    r"do(?:n't| not) see any",
    "never mentioned",
    "no information about",
)

# Compiled with word boundaries so a marker cannot fire inside a longer word.
_ABSENCE_RE = re.compile(r"\b(?:" + "|".join(_ABSENCE_MARKERS) + r")\b", re.IGNORECASE)

# A trailing clause offering the nearest thing does not undo the declaration —
# "no record of X, but you did mention Y" is FRE-1118's *target* behaviour, and
# scoring it as an assertion would penalise exactly the change being measured.
# What DOES undo it is a trailing clause answering the probe's own subject; the
# two are separated by testing the clause against the probe's subject_terms.

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE = re.compile(r"\s+")

# Negation cues. A present probe's expected token appearing inside a negated
# clause is not correct recall — "it was not wavelength or numerical aperture"
# contains every token (Codex round 1, finding 1). Negation is not decidable in
# general, so a negated token yields UNCLASSIFIABLE rather than a guess.
_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|isn'?t|wasn'?t|aren'?t|weren'?t|didn'?t|doesn'?t|don'?t)\b",
    re.IGNORECASE,
)

# Below this length a non-question sentence is a fragment ("Sure.", "Hmm."),
# not a claim about the subject. Assertion requires an actual claim.
_MIN_ASSERTION_CHARS = 12


def _normalise(text: str) -> str:
    r"""Lowercase and collapse all whitespace runs to single spaces.

    Answers arrive with line breaks and variable spacing from the renderer, so a
    naive substring test misses ``"Diffraction\\nLimit"``. Normalising both sides
    of every comparison is what makes token matching robust to formatting
    without loosening it to fuzzy matching.

    Args:
        text: Raw text.

    Returns:
        The lowercased, whitespace-collapsed form.
    """
    return _WHITESPACE.sub(" ", text).strip().lower()


def _sentences(answer: str) -> list[str]:
    """Split an answer into sentences, preserving each verbatim.

    Args:
        answer: The rendered answer.

    Returns:
        Non-empty sentences in order, each exactly as it appears in ``answer``.
    """
    return [s.strip() for s in _SENTENCE_SPLIT.split(answer.strip()) if s.strip()]


def _find_absence_span(answer: str) -> str:
    """Return the verbatim sentence declaring absence, or an empty string.

    Args:
        answer: The rendered answer.

    Returns:
        The first sentence containing an absence marker, quoted verbatim from
        ``answer``; empty if no marker is present.
    """
    for sentence in _sentences(answer):
        if _ABSENCE_RE.search(_normalise(sentence)):
            return sentence
    return ""


def _find_subject_assertion(answer: str, subject_terms: Sequence[str]) -> str:
    """Return a clause that asserts something about the probe's own subject.

    This is the discriminator that separates FRE-1118's *target* behaviour from a
    confabulation wearing its clothes. "No record of that trip, but you did
    mention Lisbon" offers a neighbouring fact and is honest absence. "No record
    of the exact date, but your sister's dog is Bramble" answers the very subject
    that has nothing stored, and must not be counted as honest.

    Args:
        answer: The rendered answer.
        subject_terms: The probe's subject terms.

    Returns:
        The first clause that names a subject term without itself being the
        absence declaration; empty when there is none.
    """
    if not subject_terms:
        return ""

    normalised_terms = [_normalise(t) for t in subject_terms if t.strip()]
    for sentence in _sentences(answer):
        for clause in re.split(r"\b(?:but|although|though|however)\b", sentence):
            normalised = _normalise(clause)
            if _ABSENCE_RE.search(normalised) or len(clause.strip()) < _MIN_ASSERTION_CHARS:
                continue
            if any(term in normalised for term in normalised_terms):
                return clause.strip()
    return ""


def _token_is_negated(answer: str, token: str) -> bool:
    """Whether an expected token appears only inside a negated clause.

    Args:
        answer: The rendered answer.
        token: One expected token.

    Returns:
        True when every clause containing the token also carries a negation cue
        ahead of it.
    """
    normalised_token = _normalise(token)
    found_any = False
    for sentence in _sentences(answer):
        for clause in re.split(r"\b(?:but|although|though|however)\b", sentence):
            normalised = _normalise(clause)
            index = normalised.find(normalised_token)
            if index < 0:
                continue
            found_any = True
            if not _NEGATION_RE.search(normalised[:index]):
                return False
    return found_any


def _find_token_span(answer: str, expected_tokens: Sequence[str]) -> str:
    """Return a verbatim span evidencing the expected tokens, or empty.

    All tokens must be present for the answer to count as reproducing the stored
    fact; a partial hit is not correct recall, because an answer naming the
    subject without the substance is the "records that a discussion happened"
    failure FRE-1115 measured on the write path.

    Args:
        answer: The rendered answer.
        expected_tokens: Text a correct answer must reproduce, from the stored
            row named in AC-2.

    Returns:
        The first sentence containing any expected token — quoted verbatim as
        the audit span — but only when *every* token appears somewhere in the
        answer. Empty otherwise.
    """
    if not expected_tokens:
        return ""

    normalised_answer = _normalise(answer)
    if not all(_normalise(token) in normalised_answer for token in expected_tokens):
        return ""

    first = _normalise(expected_tokens[0])
    for sentence in _sentences(answer):
        if first in _normalise(sentence):
            return sentence
    return answer.strip()


def _has_assertion(answer: str) -> bool:
    """Whether the answer makes any declarative claim at all.

    A bare clarifying question asserts nothing, so it is neither a confabulation
    nor an abstention — it belongs in ``unclassifiable`` rather than being
    counted against either cell.

    Args:
        answer: The rendered answer.

    Returns:
        True when at least one sentence is declarative and substantive.
    """
    return any(
        not sentence.endswith("?") and len(sentence) >= _MIN_ASSERTION_CHARS
        for sentence in _sentences(answer)
    )


def classify_answer(
    answer: str,
    *,
    status: ProbeStatus,
    expected_tokens: Sequence[str],
    subject_terms: Sequence[str] = (),
) -> Classification:
    """Classify one rendered answer into exactly one outcome.

    Crossed with the probe's known status, the outcome yields the cells the
    fixture reports: correct recall, confabulation over truth, false absence,
    confabulation on nothing, and honest absence.

    On an ``absent`` probe ``ASSERTED_CORRECT`` is unreachable by construction.
    AC-7 requires every absent probe to be personally scoped and unobtainable by
    any route, so no correct answer exists to be produced — from the store or
    from the model's own weights. Returning it there would be scoring parametric
    knowledge as recall, which is the confound AC-7 exists to remove.

    Args:
        answer: The answer text as rendered to the user.
        status: The probe's construction-time ground truth.
        expected_tokens: For a ``present`` probe, the text a correct answer must
            reproduce, taken from the stored row named in AC-2. Must be empty
            for an ``absent`` probe — there is nothing correct to match.
        subject_terms: The probe's subject terms. Load-bearing on the absent
            half: they are what separates a nearest-thing offer from a
            confabulation that answers the absent subject behind an absence
            clause. Omitting them weakens the absent half's classification.

    Returns:
        The classification with the verbatim span or the reason that decided it.

    Raises:
        ValueError: If ``expected_tokens`` is supplied for an absent probe,
            which would imply a correct answer exists and contradict AC-7.
    """
    if status == "absent" and expected_tokens:
        raise ValueError(
            "an absent probe cannot carry expected_tokens: AC-7 requires its "
            "subject be unobtainable by any route, so no correct answer exists"
        )

    if not answer.strip():
        return Classification(
            outcome=Outcome.UNCLASSIFIABLE,
            evidence_span="",
            reason="the answer is empty; nothing was asserted or declared",
        )

    absence_span = _find_absence_span(answer)
    token_span = _find_token_span(answer, expected_tokens)

    # A present probe's token inside a negated clause ("it was NOT wavelength")
    # is not correct recall. Negation is not decidable in general, so this is
    # reported undecided rather than guessed either way.
    negated = [t for t in expected_tokens if _token_is_negated(answer, t)]
    if token_span and negated:
        return Classification(
            outcome=Outcome.UNCLASSIFIABLE,
            evidence_span=token_span,
            reason=f"expected token(s) {negated!r} appear only inside a negated clause",
        )

    # Both signals fired. Neither cell is defensible, and picking one by
    # precedence would inflate it with genuinely ambiguous answers (AC-4).
    if absence_span and token_span:
        return Classification(
            outcome=Outcome.UNCLASSIFIABLE,
            evidence_span=absence_span,
            reason=(
                "the answer both reproduces the expected content and declares "
                f"absence; assertion span: {token_span!r}"
            ),
        )

    # An absence clause followed by an assertion about *this probe's subject* is
    # a confabulation wearing an abstention's clothes. Before this check the
    # ambiguity rule above was unreachable on the absent half — absent probes
    # carry no expected tokens — so every such answer scored as honest absence
    # and inflated the one number this fixture produces.
    if absence_span:
        subject_assertion = _find_subject_assertion(answer, subject_terms)
        if subject_assertion:
            return Classification(
                outcome=Outcome.UNCLASSIFIABLE,
                evidence_span=absence_span,
                reason=(
                    "the answer declares absence and then asserts something about "
                    f"the probe's own subject: {subject_assertion!r}"
                ),
            )

    if absence_span:
        return Classification(
            outcome=Outcome.DECLARED_ABSENCE,
            evidence_span=absence_span,
            reason=(
                "honest absence on a probe with nothing stored"
                if status == "absent"
                else "false absence: the fact is stored and was verified present before the run"
            ),
        )

    if token_span:
        return Classification(
            outcome=Outcome.ASSERTED_CORRECT,
            evidence_span=token_span,
            reason="the answer reproduces every expected token from the stored row",
        )

    if not _has_assertion(answer):
        return Classification(
            outcome=Outcome.UNCLASSIFIABLE,
            evidence_span=answer.strip(),
            reason="the answer makes no declarative claim (a clarifying question or a fragment)",
        )

    return Classification(
        outcome=Outcome.ASSERTED_WRONG,
        evidence_span=_sentences(answer)[0],
        reason=(
            "a confident assertion on a subject with nothing stored"
            if status == "absent"
            else "an assertion that does not reproduce the stored fact"
        ),
    )
