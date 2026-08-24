"""Labelled-corpus schema, loader and discipline guards (FRE-1281, ADR-0138 AC-7).

**Offsets are derived, never hand-written.** A labeller writes the exact quoted text and,
where it recurs, which occurrence is meant; the loader resolves that to ``(start, end)``
and validates it. Hand-authored character offsets in a 130-span YAML file would be wrong
within a week of the first edit, and wrong silently — every span would still load, just
pointing somewhere else.

**The guards here are what make the bar-floor arithmetic true.** ``bars.py`` claims that
two of its five broken baselines fail *by construction*: ``entity_triggered`` cannot find
a bare predicate, and ``accept_all`` cannot reach the precision bar. Neither claim is
secured by labelling intention — a plan review broke both. So:

- :data:`BARE_PREDICATE_CLASS` spans are rejected at load if they contain a digit or a
  capitalised non-initial token, which is what makes them invisible to an
  entity-or-figure trigger.
- :data:`MIN_EXEMPT_FRACTION` bounds ``accept_all``'s precision ceiling below the
  precision bar arithmetically (see :func:`exempt_fraction`).

Both are load-time invariants rather than review conventions, because a corpus that
quietly stops satisfying them turns a bar into decoration without failing anything.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

CORPUS_SCHEMA_VERSION = 1
"""Bumped when the YAML shape changes in a way old files would not survive."""

DEFAULT_CORPUS_PATH = Path("scripts/eval/fre1281_span_extraction/corpus.yaml")


class SpanLabel(StrEnum):
    """The three things a segment can be.

    ``NOT_A_CLAIM`` is a decision, not a gap (ADR-0138 D1 via the coverage contract): it
    records that the text was examined and judged to make no claim about the world. An
    extractor that simply omits text produces no segment at all, which is the seam the
    tiling requirement closes.
    """

    CLAIM_EXEMPT = "claim_exempt"
    CLAIM_NON_EXEMPT = "claim_non_exempt"
    NOT_A_CLAIM = "not_a_claim"


class SpanClass(StrEnum):
    """The thirteen classes AC-2 and AC-3 are reported over.

    Eight non-exempt, five exempt. Every one is a region or a rule named in ADR-0138 D1;
    none was invented to make a number look better.
    """

    # Non-exempt — a citation is required.
    FACTUAL_ENTITY = "factual_entity"
    FACTUAL_BARE_PREDICATE = "factual_bare_predicate"
    PROSE_IN_FENCE = "prose_in_fence"
    NL_IN_CODE = "nl_in_code"
    DEPENDENCY_DECLARATION = "dependency_declaration"
    PROSE_ABOUT_CODE = "prose_about_code"
    CHECKABLE_EVALUATIVE = "checkable_evaluative"
    UNATTRIBUTED_RESTATEMENT = "unattributed_restatement"
    # Exempt.
    CODE_BODY = "code_body"
    ATTRIBUTED_RESTATEMENT = "attributed_restatement"
    DERIVED_ARITHMETIC = "derived_arithmetic"
    CONNECTIVE_EVALUATIVE = "connective_evaluative"
    SYSTEM_RECORD = "system_record"


NON_EXEMPT_CLASSES: frozenset[SpanClass] = frozenset(
    {
        SpanClass.FACTUAL_ENTITY,
        SpanClass.FACTUAL_BARE_PREDICATE,
        SpanClass.PROSE_IN_FENCE,
        SpanClass.NL_IN_CODE,
        SpanClass.DEPENDENCY_DECLARATION,
        SpanClass.PROSE_ABOUT_CODE,
        SpanClass.CHECKABLE_EVALUATIVE,
        SpanClass.UNATTRIBUTED_RESTATEMENT,
    }
)

EXEMPT_CLASSES: frozenset[SpanClass] = frozenset(set(SpanClass) - set(NON_EXEMPT_CLASSES))

BARE_PREDICATE_CLASS = SpanClass.FACTUAL_BARE_PREDICATE
"""The class whose definition makes ``entity_triggered`` provably broken.

"This fish is high in mercury" is a checkable factual claim containing no named entity,
and it escaped the draft of D1 that review rejected. A baseline firing on an entity *or a
figure* only fails to find this class if the class contains neither — hence the load-time
guard rather than a labelling note.
"""

MIN_SPANS_PER_CLASS = 10
"""Floor per class, chosen so the bars are bars.

At 6 examples a ≥0.85 recall bar means 6/6 and a ≤0.15 false-positive bar means 0/6 —
both collapse to perfection tests, and whether a single error is survivable becomes an
artefact of how many examples a class happened to get. At 10, each bar tolerates exactly
one error.
"""

MIN_EXEMPT_FRACTION = 0.30
"""Exempt share of labelled claim spans, bounding ``accept_all``'s precision.

``accept_all`` marks every claim span non-exempt, so its precision is at most
``non_exempt / (non_exempt + exempt)``. Holding exempt at ≥0.30 caps that at 0.70, below
the 0.80 precision bar — by arithmetic, not by hoping the counts come out right.
"""

#: Tokens that would indicate leaked private content in a public repo. Mirrors the
#: FRE-489 / FRE-630 denylist, with one deliberate difference: that list carries a bare
#: ``"@"`` to catch email addresses, which is too blunt here because this corpus contains
#: real code and a decorator is not a leak. :data:`EMAIL_PATTERN` carries that intent
#: precisely instead, so the guard keeps its teeth without failing on ``@click.command()``.
PII_DENYLIST: frozenset[str] = frozenset({"kookier", "icloud.com", "cf-access", "starry-plaza"})

#: What the bare ``"@"`` was actually protecting against.
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_DIGIT = re.compile(r"\d")
_CAPITALISED_NON_INITIAL = re.compile(r"(?<!^)(?<![.!?]\s)\b[A-Z]\w*")


class Partition(StrEnum):
    """Which half of the held-out discipline a document belongs to.

    ``DEV`` may be inspected per-document while iterating. ``HELDOUT`` is reported in
    aggregate only — ``report.py`` cannot emit its per-document diffs, which is a property
    of the reporter rather than a promise by the author (ADR-0138's governance note: "An
    implementation cannot special-case probes it has not seen").
    """

    DEV = "dev"
    HELDOUT = "heldout"


class GoldSpan(BaseModel):
    """One labelled segment of a document.

    Attributes:
        text: The exact substring as it appears in the document.
        label: Whether it is an exempt claim, a non-exempt claim, or no claim.
        span_class: Which of the thirteen classes it belongs to. ``None`` only for
            ``NOT_A_CLAIM`` segments, which have no class to report.
        occurrence: 1-based index of which occurrence of ``text`` is meant, for text that
            recurs — AC-4's own probe repeats a package name deliberately.
        note: Labeller's rationale, for the disagreements ``ADJUDICATION.md`` records.
        start: Derived by the loader.
        end: Derived by the loader, exclusive.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    label: SpanLabel
    span_class: SpanClass | None = None
    occurrence: int = 1
    note: str | None = None
    start: int = -1
    end: int = -1

    @model_validator(mode="after")
    def _class_matches_label(self) -> Self:
        """Reject a class/label pairing the taxonomy forbids.

        Raises:
            ValueError: If the class and label disagree, or a claim carries no class.
        """
        if self.label is SpanLabel.NOT_A_CLAIM:
            if self.span_class is not None:
                raise ValueError(f"NOT_A_CLAIM span carries class {self.span_class!r}")
            return self
        if self.span_class is None:
            raise ValueError(f"claim span {self.text!r} carries no class")
        expected = (
            SpanLabel.CLAIM_NON_EXEMPT
            if self.span_class in NON_EXEMPT_CLASSES
            else SpanLabel.CLAIM_EXEMPT
        )
        if self.label is not expected:
            raise ValueError(
                f"class {self.span_class.value!r} implies {expected.value!r}, "
                f"but span {self.text!r} is labelled {self.label.value!r}"
            )
        return self

    @model_validator(mode="after")
    def _bare_predicate_is_actually_bare(self) -> Self:
        """Enforce the property that makes ``entity_triggered`` provably fail.

        Raises:
            ValueError: If a bare-predicate span carries a digit or a named entity.
        """
        if self.span_class is not BARE_PREDICATE_CLASS:
            return self
        if _DIGIT.search(self.text):
            raise ValueError(
                f"{BARE_PREDICATE_CLASS.value} span {self.text!r} contains a digit — an "
                f"entity-or-figure trigger would find it, so it cannot prove that "
                f"baseline broken"
            )
        if _CAPITALISED_NON_INITIAL.search(self.text):
            raise ValueError(
                f"{BARE_PREDICATE_CLASS.value} span {self.text!r} contains a capitalised "
                f"non-initial token, which reads as a named entity"
            )
        return self


class GoldDocument(BaseModel):
    """One model output, fully tiled by labelled spans.

    Attributes:
        doc_id: Stable identifier, unique across the corpus.
        partition: ``dev`` (inspectable) or ``heldout`` (aggregate-only).
        text: The model output being labelled.
        user_message: The user turn this responded to. Required wherever a restatement
            class appears — attribution cannot be judged without it.
        spans: The labelled segments, in document order.
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    partition: Partition
    text: str
    user_message: str | None = None
    spans: tuple[GoldSpan, ...]

    @property
    def claim_spans(self) -> tuple[GoldSpan, ...]:
        """Spans that assert something — the scoring population."""
        return tuple(s for s in self.spans if s.label is not SpanLabel.NOT_A_CLAIM)


def _whitespace_tolerant(quote: str) -> re.Pattern[str]:
    """Compile a quote so any run of whitespace matches any other.

    A labeller quotes a span from a wrapped YAML block scalar and cannot know where the
    line breaks land; requiring an exact match makes every reflow of the file a silent
    trap. Whitespace is the only variance tolerated — the words themselves must match
    exactly, so this cannot quietly anchor a different claim.

    Args:
        quote: The labeller's span text.

    Returns:
        A compiled pattern matching that text with flexible internal whitespace.

    Raises:
        ValueError: If the quote is empty or whitespace only.
    """
    tokens = quote.split()
    if not tokens:
        raise ValueError("span text is empty or whitespace only")
    return re.compile(r"\s+".join(re.escape(token) for token in tokens))


def _anchor(document_text: str, span: GoldSpan, doc_id: str) -> GoldSpan:
    """Resolve a span's quoted text to character offsets.

    The stored ``text`` is replaced by the document's own substring, so ``text`` and
    ``(start, end)`` can never disagree downstream.

    Args:
        document_text: The document the span is quoted from.
        span: The span carrying ``text`` and ``occurrence``.
        doc_id: For the error message.

    Returns:
        The span with ``start``, ``end`` and the document's exact ``text``.

    Raises:
        ValueError: If the quote is absent, or has fewer occurrences than requested.
    """
    if span.occurrence < 1:
        raise ValueError(f"{doc_id}: occurrence must be 1-based, got {span.occurrence}")
    try:
        pattern = _whitespace_tolerant(span.text)
    except ValueError as exc:
        raise ValueError(f"{doc_id}: {exc}") from exc

    matches = list(pattern.finditer(document_text))
    if len(matches) < span.occurrence:
        raise ValueError(
            f"{doc_id}: span {span.text!r} occurrence {span.occurrence} not found in "
            f"document text ({len(matches)} occurrence(s) present)"
        )
    match = matches[span.occurrence - 1]
    return span.model_copy(
        update={"start": match.start(), "end": match.end(), "text": match.group(0)}
    )


def _reject_overlaps(spans: Sequence[GoldSpan], doc_id: str) -> None:
    """Reject spans that overlap or nest.

    Args:
        spans: Anchored spans, any order.
        doc_id: For the error message.

    Raises:
        ValueError: On the first overlapping pair found.
    """
    ordered = sorted(spans, key=lambda s: (s.start, s.end))
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if later.start < earlier.end:
            raise ValueError(
                f"{doc_id}: spans overlap — {earlier.text!r} [{earlier.start}:{earlier.end}] "
                f"and {later.text!r} [{later.start}:{later.end}]. ADR-0138 D1 requires "
                f"non-overlapping atomic claims."
            )


def load_corpus(path: Path | None = None) -> tuple[GoldDocument, ...]:
    """Load, anchor and validate the labelled corpus.

    Args:
        path: Corpus YAML; defaults to :data:`DEFAULT_CORPUS_PATH`.

    Returns:
        The documents, with every span anchored to character offsets.

    Raises:
        ValueError: On a schema-version mismatch, a duplicate ``doc_id``, an unanchorable
            span, an overlapping pair, or a restatement class with no ``user_message``.
    """
    corpus_path = DEFAULT_CORPUS_PATH if path is None else path
    raw = yaml.safe_load(corpus_path.read_text(encoding="utf-8"))

    version = raw.get("schema_version")
    if version != CORPUS_SCHEMA_VERSION:
        raise ValueError(
            f"{corpus_path}: schema_version {version!r} != {CORPUS_SCHEMA_VERSION} — the "
            f"loader would misread this file rather than fail on it"
        )

    documents: list[GoldDocument] = []
    seen: set[str] = set()
    for entry in raw.get("documents", []):
        document = GoldDocument.model_validate(entry)
        if document.doc_id in seen:
            raise ValueError(f"duplicate doc_id {document.doc_id!r}")
        seen.add(document.doc_id)

        anchored = tuple(_anchor(document.text, s, document.doc_id) for s in document.spans)
        _reject_overlaps(anchored, document.doc_id)

        needs_user = {SpanClass.ATTRIBUTED_RESTATEMENT, SpanClass.UNATTRIBUTED_RESTATEMENT}
        if any(s.span_class in needs_user for s in anchored) and not document.user_message:
            raise ValueError(
                f"{document.doc_id}: carries a restatement class but no user_message — "
                f"attribution is undecidable without the user's own words"
            )
        documents.append(document.model_copy(update={"spans": anchored}))

    if not documents:
        raise ValueError(f"{corpus_path}: corpus is empty")
    return tuple(documents)


def class_counts(documents: Sequence[GoldDocument]) -> dict[SpanClass, int]:
    """Count gold claim spans per class.

    Args:
        documents: Loaded corpus.

    Returns:
        One entry per class present; classes with no spans are absent.
    """
    counts: dict[SpanClass, int] = {}
    for document in documents:
        for span in document.claim_spans:
            if span.span_class is not None:
                counts[span.span_class] = counts.get(span.span_class, 0) + 1
    return counts


def exempt_fraction(documents: Sequence[GoldDocument]) -> float:
    """Exempt share of claim spans — the term bounding ``accept_all``'s precision.

    Args:
        documents: Loaded corpus.

    Returns:
        ``exempt / (exempt + non_exempt)`` over claim spans.

    Raises:
        ValueError: If the corpus holds no claim spans at all.
    """
    exempt = 0
    total = 0
    for document in documents:
        for span in document.claim_spans:
            total += 1
            if span.label is SpanLabel.CLAIM_EXEMPT:
                exempt += 1
    if total == 0:
        raise ValueError("corpus holds no claim spans — the fraction is undefined")
    return exempt / total


def all_authored_strings(documents: Sequence[GoldDocument]) -> list[str]:
    """Every string a labeller wrote, for the PII guard.

    Args:
        documents: Loaded corpus.

    Returns:
        Document texts, user messages, span texts and notes.
    """
    strings: list[str] = []
    for document in documents:
        strings.append(document.text)
        if document.user_message:
            strings.append(document.user_message)
        for span in document.spans:
            strings.append(span.text)
            if span.note:
                strings.append(span.note)
    return strings
