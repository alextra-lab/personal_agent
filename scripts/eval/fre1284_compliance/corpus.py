"""Labelled turn-compliance corpus: schema, loader and load-time guards (FRE-1284 AC-1).

**Offsets are derived, never hand-written**, following FRE-1281's corpus: a labeller
writes the exact quoted text, the loader resolves it to ``(start, end)`` against the
citation-substituted reply and fails loudly when it cannot. Hand-authored offsets would go
wrong within one edit, and silently — every span would still load, pointing elsewhere.

**The guards here are what keep AC-1 from being decorative.** A corpus that quietly stops
containing a mixed turn, or stops containing a compliant one, turns "the metric agrees with
independent labelling" into a statement about a degenerate set:

- :func:`Corpus.validate_discriminating` requires at least one turn labelled compliant,
  one labelled non-compliant, one outside the denominator, and — the case AC-1 names — at
  least one turn with two or more non-exempt spans where exactly one carries a citation.
  Without that last document, an "at least one citation is present" implementation scores
  a perfect agreement.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

CORPUS_SCHEMA_VERSION = 1
"""Bumped when the YAML shape changes in a way old files would not survive."""

DEFAULT_CORPUS_PATH = Path(__file__).parent / "corpus.yaml"

UNRESOLVED_REF = "UNKNOWN"
"""Reserved placeholder for a well-formed marker that resolves to no registered source."""


class Partition(StrEnum):
    """Which half of the corpus a document belongs to."""

    DEV = "dev"
    HELDOUT = "heldout"


class SourceKind(StrEnum):
    """How a document's source is registered.

    Only the two kinds this corpus needs. ``TOOL`` goes through
    ``register_tool_result``; ``MEMORY`` through ``register_memory_item``, which is the
    path that carries ``asserted_by`` and therefore D2's entitlement decision.
    """

    TOOL = "tool"
    MEMORY = "memory"


class LabelledSource(BaseModel):
    """One source the turn had in hand.

    Attributes:
        ref: The placeholder name used in the reply text, e.g. ``S1`` for ``{{S1}}``.
        kind: Which registration path to use.
        tool: Tool name, for ``TOOL`` sources.
        arguments: The model's arguments to that call — never admissible content.
        asserted_by: ``user`` or ``agent``, for ``MEMORY`` sources. Absence is not
            neutral: the registry resolves it to agent-derived, default-deny.
        identity: The memory item's identity, for ``MEMORY`` sources.
        content: The retrieved content.
    """

    model_config = ConfigDict(frozen=True)

    ref: str
    kind: SourceKind
    tool: str | None = None
    arguments: dict[str, str] = {}
    asserted_by: str | None = None
    identity: str | None = None
    content: str

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        """Reject a source whose kind and fields disagree.

        Returns:
            The validated source.

        Raises:
            ValueError: When a tool source names no tool, or a memory source names one.
        """
        if self.kind is SourceKind.TOOL and not self.tool:
            raise ValueError(f"tool source {self.ref} names no tool")
        if self.kind is SourceKind.MEMORY and self.tool:
            raise ValueError(f"memory source {self.ref} names a tool")
        return self


class LabelledSpan(BaseModel):
    """One hand-labelled segment of the reply.

    Attributes:
        text: The exact quoted text, citation markers excluded.
        label: ``claim_non_exempt``, ``claim_exempt`` or ``not_a_claim``.
        region: Which D1 region excuses it. Required for, and only for, exempt spans.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    label: str
    region: str | None = None

    @model_validator(mode="after")
    def _region_matches_label(self) -> Self:
        """Mirror the production ``Span`` invariant at load time.

        Returns:
            The validated span.

        Raises:
            ValueError: When an exempt span names no region, or a non-exempt one does.
        """
        if self.label == "claim_exempt" and not self.region:
            raise ValueError(f"exempt span {self.text!r} names no region")
        if self.label != "claim_exempt" and self.region:
            raise ValueError(f"span {self.text!r} is {self.label} but names a region")
        return self

    @property
    def non_exempt(self) -> bool:
        """Whether this span carries a citation obligation."""
        return self.label == "claim_non_exempt"


class LabelledTurn(BaseModel):
    """One turn, with the independent labelling the metric is scored against.

    Attributes:
        doc_id: Stable identifier.
        partition: dev or heldout.
        user_message: What was asked.
        sources: What the turn had retrieved.
        text: The reply, with ``{{ref}}`` citation placeholders.
        spans: The hand-labelled segments.
        denominator: Whether this turn belongs in the metric's denominator — hand-labelled
            rather than derived, so a turn that should have been counted and was not is a
            disagreement rather than an omission nobody notices.
        compliant: The hand-authored turn-level label. ``None`` exactly when the turn is
            outside the denominator.
        note: Why the label is what it is.
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    partition: Partition
    user_message: str
    sources: tuple[LabelledSource, ...] = ()
    text: str
    spans: tuple[LabelledSpan, ...]
    denominator: bool
    compliant: bool | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _label_matches_denominator(self) -> Self:
        """Tie the two labels together so neither can drift alone.

        Returns:
            The validated turn.

        Raises:
            ValueError: When the compliance label and the denominator label disagree, or
                when the denominator label contradicts the span labelling.
        """
        has_non_exempt = any(span.non_exempt for span in self.spans)
        if self.denominator != has_non_exempt:
            raise ValueError(
                f"{self.doc_id}: denominator={self.denominator} but "
                f"{'a' if has_non_exempt else 'no'} non-exempt span is labelled"
            )
        if self.denominator and self.compliant is None:
            raise ValueError(f"{self.doc_id}: in the denominator but carries no label")
        if not self.denominator and self.compliant is not None:
            raise ValueError(f"{self.doc_id}: outside the denominator but carries a label")
        return self

    @property
    def non_exempt_spans(self) -> tuple[LabelledSpan, ...]:
        """The spans a citation is owed for."""
        return tuple(span for span in self.spans if span.non_exempt)


class Corpus(BaseModel):
    """The whole labelled set.

    Attributes:
        schema_version: Shape version.
        corpus_version: Content version, bumped on every label change.
        documents: The turns.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int
    corpus_version: str
    documents: tuple[LabelledTurn, ...]

    def partition(self, partition: Partition) -> tuple[LabelledTurn, ...]:
        """Return one partition's documents.

        Args:
            partition: Which half.

        Returns:
            The documents in it.
        """
        return tuple(doc for doc in self.documents if doc.partition is partition)

    def validate_discriminating(self) -> None:
        """Assert the corpus can actually separate a correct metric from a broken one.

        Raises:
            ValueError: When the set is degenerate in any of the four ways that would let
                a wrong implementation score perfect agreement.
        """
        if not any(doc.compliant is True for doc in self.documents):
            raise ValueError("no compliant turn: an always-false metric would score perfectly")
        if not any(doc.compliant is False for doc in self.documents):
            raise ValueError("no non-compliant turn: an always-true metric would score perfectly")
        if not any(not doc.denominator for doc in self.documents):
            raise ValueError("no turn outside the denominator: the exclusion rule is untested")

        mixed = [
            doc
            for doc in self.documents
            if len(doc.non_exempt_spans) >= 2 and doc.compliant is False
        ]
        if not mixed:
            raise ValueError(
                "no turn with several non-exempt spans labelled non-compliant: an "
                "'at least one citation' implementation would score perfect agreement"
            )


def load_corpus(path: Path | None = None) -> Corpus:
    """Load and validate the labelled corpus.

    Args:
        path: Corpus file. Defaults to the one beside this module.

    Returns:
        The validated corpus.

    Raises:
        ValueError: On an unsupported schema version, or a degenerate set.
    """
    raw = yaml.safe_load((path or DEFAULT_CORPUS_PATH).read_text(encoding="utf-8"))
    corpus = Corpus.model_validate(raw)
    if corpus.schema_version != CORPUS_SCHEMA_VERSION:
        raise ValueError(
            f"corpus schema_version {corpus.schema_version} != {CORPUS_SCHEMA_VERSION}"
        )
    corpus.validate_discriminating()
    return corpus


def locate(text: str, quoted: str) -> tuple[int, int]:
    """Resolve a labelled quote to offsets in the reply.

    Args:
        text: The citation-substituted reply.
        quoted: The exact labelled text.

    Returns:
        ``(start, end)`` such that ``text[start:end] == quoted``.

    Raises:
        ValueError: When the quote is absent, or occurs more than once — an ambiguous
            quote would silently anchor to the wrong occurrence.
    """
    first = text.find(quoted)
    if first < 0:
        raise ValueError(f"labelled span not found in reply: {quoted!r}")
    if text.find(quoted, first + 1) >= 0:
        raise ValueError(f"labelled span occurs more than once: {quoted!r}")
    return first, first + len(quoted)


__all__ = [
    "CORPUS_SCHEMA_VERSION",
    "DEFAULT_CORPUS_PATH",
    "UNRESOLVED_REF",
    "Corpus",
    "LabelledSource",
    "LabelledSpan",
    "LabelledTurn",
    "Partition",
    "SourceKind",
    "load_corpus",
    "locate",
]


def document_ids(documents: Sequence[LabelledTurn]) -> tuple[str, ...]:
    """Return the ids of a document sequence, for readable failure messages.

    Args:
        documents: The documents.

    Returns:
        Their ids, in order.
    """
    return tuple(doc.doc_id for doc in documents)
