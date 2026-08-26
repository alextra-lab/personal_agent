"""The labelled entailment corpus (ADR-0138 D3(d), FRE-1286 AC-6).

ADR-0138 rejected Option 5 partly because "the entailment judge is itself a model, with its
own error rate, sitting on the critical path". Promoting it inline for one class — which
D3(d) does — does not make that objection go away; it makes measuring the judge a
precondition. **An unmeasured judge on the critical path is the weakest-link failure Option
5 was rejected for**, so this corpus exists before the judge is trusted, not after.

**Four classes, and two of them are the ADR's own named residue.** Containment accepts
contradiction and quantifier reversal — a source saying *"not sold in France"* contains
every token of *"sold in France"*, and *"some"* passes for *"all"*. Those are recorded in
ADR-0138 as accepted residual risk assigned to this ticket, so they are scored as their own
classes rather than being averaged into one accuracy number where a systematic blindness to
either would disappear.

**``supported`` is a class, not a control.** A judge that answers ``not_supported`` to
everything scores perfectly on the three negative classes. The false-rejection rate over
this class is what makes that judge fail, and it is also the number that matters in
production: under D4 a false rejection costs a refusal the user did not deserve.

**Partitions.** ``dev`` is for iterating on the prompt; ``heldout`` is scored once, after
the judge is frozen. Reporting a figure tuned against the same cases it was measured on is
the way an eval quietly becomes a rehearsal — the split FRE-1281 used for the span
extractor, for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

CORPUS_PATH = Path(__file__).parent / "corpus.yaml"


class CaseClass(StrEnum):
    """What a case is testing.

    ``QUANTIFIER_REVERSAL`` is separated from the two verdict-shaped classes because it is
    a *reason* a judge fails rather than a verdict: its expected answer is sometimes
    ``not_supported`` (*some* offered for *all*) and sometimes ``contradicted`` (*no* against
    *all*), and collapsing it into either would hide the blindness the class exists to find.
    """

    SUPPORTED = "supported"
    NOT_SUPPORTED = "not_supported"
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
        if case_class is CaseClass.SUPPORTED and expected != "supported":
            raise CorpusError(f"{case_id}: a supported case cannot expect {expected}")
        if case_class is CaseClass.CONTRADICTED and expected != "contradicted":
            raise CorpusError(f"{case_id}: a contradicted case cannot expect {expected}")

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
