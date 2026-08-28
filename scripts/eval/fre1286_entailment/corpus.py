"""The labelled entailment corpus (ADR-0138 D3(d), FRE-1286 AC-6, FRE-1301).

ADR-0138 rejected Option 5 partly because "the entailment judge is itself a model, with its
own error rate, sitting on the critical path". Promoting it inline for one class — which
D3(d) does — does not make that objection go away; it makes measuring the judge a
precondition. **An unmeasured judge on the critical path is the weakest-link failure Option
5 was rejected for**, so this corpus exists before the judge is trusted, not after.

**Five classes, and three of them are the ADR's own named residue or its consequence.**
Containment accepts contradiction and quantifier reversal — a source saying *"not sold in
France"* contains every token of *"sold in France"*, and *"some"* passes for *"all"*. Those
are recorded in ADR-0138 as accepted residual risk, so they are scored as their own classes
rather than being averaged into one accuracy number where a systematic blindness to either
would disappear. ``not_supported`` was originally one such class too, until FRE-1301's
held-out run found it conflating two different things (see :class:`CaseClass`) and split it
into ``silent`` and ``implicitly_refuted``.

**``supported`` is a class, not a control.** A judge that answers ``not_supported`` to
everything scores perfectly on the negative classes. The false-rejection rate over this
class is what makes that judge fail, and it is also the number that matters in production:
under D4 a false rejection costs a refusal the user did not deserve.

**Partitions.** ``dev`` is for iterating on the prompt; ``heldout`` is scored once, after
the judge is frozen. Reporting a figure tuned against the same cases it was measured on is
the way an eval quietly becomes a rehearsal — the split FRE-1281 used for the span
extractor, for the same reason. FRE-1286's 2026-08-26 run scored every case then in
``heldout``; none of those may serve as held-out again, so FRE-1301 moved them to ``dev``
and authored a fresh ``heldout`` set.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

CORPUS_PATH = Path(__file__).parent / "corpus.yaml"


class CaseClass(StrEnum):
    """What a case is testing.

    ``SILENT`` and ``IMPLICITLY_REFUTED`` replace what was originally one ``not_supported``
    class (FRE-1301). Both are drawn from the judge's own stated definitions: ``SILENT`` is
    a passage that "neither entails the claim nor its negation"; ``IMPLICITLY_REFUTED`` is a
    passage that "states or directly entails the NEGATION of the claim" without an explicit
    negating word or a directly conflicting stated value — the shape :attr:`CONTRADICTED`
    cases already have. Kept apart from ``CONTRADICTED`` rather than merged into it because
    the whole point is telemetry that can tell "the judge misses plainly-worded refutation"
    from "the judge misses refutation it has to infer" — collapsing them back together would
    erase the distinction the split exists to measure.

    ``QUANTIFIER_REVERSAL`` is separated from the verdict-shaped classes because it is a
    *reason* a judge fails rather than a verdict: its expected answer is sometimes
    ``not_supported`` (*some* offered for *all*), sometimes ``contradicted`` (*no* against
    *all*) and sometimes ``supported`` (*all* offered for *some*), and collapsing it into any
    one of those would hide the blindness the class exists to find.
    """

    SUPPORTED = "supported"
    SILENT = "silent"
    IMPLICITLY_REFUTED = "implicitly_refuted"
    CONTRADICTED = "contradicted"
    QUANTIFIER_REVERSAL = "quantifier_reversal"


class Partition(StrEnum):
    """Which half of the corpus a case belongs to."""

    DEV = "dev"
    HELDOUT = "heldout"


@dataclass(frozen=True)
class EntailmentCase:
    """One labelled claim/passage pair.

    Attributes:
        id: Stable identifier, so a regression names the case that moved.
        claim: The asserted span, as a model would have written it.
        passage: The cited source's content.
        expected: The verdict a correct judge returns — ``supported``, ``not_supported``
            or ``contradicted``. Never ``undecided``: that is the judge failing, and no
            case is labelled to expect a failure.
        case_class: What this case is testing.
        partition: ``dev`` or ``heldout``.
        note: Why the case is here, for whoever reads a failure.
    """

    id: str
    claim: str
    passage: str
    expected: str
    case_class: CaseClass
    partition: Partition
    note: str


class CorpusError(ValueError):
    """Raised when the corpus file does not describe a scoreable set."""


_EXPECTED_VERDICTS = frozenset({"supported", "not_supported", "contradicted"})

_CLASS_EXPECTED: dict[CaseClass, str] = {
    CaseClass.SUPPORTED: "supported",
    CaseClass.SILENT: "not_supported",
    CaseClass.IMPLICITLY_REFUTED: "contradicted",
    CaseClass.CONTRADICTED: "contradicted",
}
"""The boundary between ``silent`` and ``implicitly_refuted`` (FRE-1301 AC-1), stated as a
mechanical map from class to the one verdict it may expect — the same device already used
for ``supported`` and ``contradicted``. ``QUANTIFIER_REVERSAL`` is deliberately absent: its
expected verdict varies by case, so no fixed mapping could apply to it.
"""


def load_corpus(path: Path | None = None) -> tuple[EntailmentCase, ...]:
    """Load and validate the labelled corpus.

    Args:
        path: Corpus file. Defaults to the committed :data:`CORPUS_PATH`.

    Returns:
        Every case, in file order.

    Raises:
        CorpusError: On a duplicate id, an unknown class or partition, a verdict outside
            the closed set, or a class whose label contradicts it — a ``supported`` case
            expecting anything but ``supported`` is a labelling error, and finding it at
            load time beats discovering it as an unexplained score.
    """
    raw = yaml.safe_load((path or CORPUS_PATH).read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        raise CorpusError("corpus must be a mapping with a 'cases' list")

    cases: list[EntailmentCase] = []
    seen: set[str] = set()
    for entry in raw["cases"]:
        if not isinstance(entry, dict):
            raise CorpusError(f"case is not a mapping: {entry!r}")
        case_id = str(entry.get("id", ""))
        if not case_id:
            raise CorpusError("every case needs an id")
        if case_id in seen:
            raise CorpusError(f"duplicate case id: {case_id}")
        seen.add(case_id)

        try:
            case_class = CaseClass(str(entry.get("class", "")))
            partition = Partition(str(entry.get("partition", "")))
        except ValueError as exc:
            raise CorpusError(f"{case_id}: {exc}") from exc

        expected = str(entry.get("expected", ""))
        if expected not in _EXPECTED_VERDICTS:
            raise CorpusError(f"{case_id}: expected must be one of {sorted(_EXPECTED_VERDICTS)}")
        required = _CLASS_EXPECTED.get(case_class)
        if required is not None and expected != required:
            raise CorpusError(f"{case_id}: a {case_class.value} case cannot expect {expected}")

        for field in ("claim", "passage", "note"):
            if not str(entry.get(field, "")).strip():
                raise CorpusError(f"{case_id}: {field} must not be empty")

        cases.append(
            EntailmentCase(
                id=case_id,
                claim=str(entry["claim"]),
                passage=str(entry["passage"]),
                expected=expected,
                case_class=case_class,
                partition=partition,
                note=str(entry["note"]),
            )
        )

    if not cases:
        raise CorpusError("corpus is empty")
    return tuple(cases)


def partitioned(
    cases: tuple[EntailmentCase, ...], partition: Partition | None
) -> tuple[EntailmentCase, ...]:
    """Filter cases to one partition.

    Args:
        cases: The loaded corpus.
        partition: The partition, or None for the whole corpus.

    Returns:
        The selected cases.
    """
    if partition is None:
        return cases
    return tuple(case for case in cases if case.partition is partition)


__all__ = [
    "CORPUS_PATH",
    "CaseClass",
    "CorpusError",
    "EntailmentCase",
    "Partition",
    "load_corpus",
    "partitioned",
]
