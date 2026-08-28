"""Score the D5 compliance metric against independent labelling (FRE-1284 AC-1).

**What makes this non-circular.** The spans come from the corpus's hand labelling, not
from the span extractor — ADR-0138 AC-1's own rule, since scoring the extractor's output
against a metric built on that output measures nothing ("an extractor that recognises
nothing would trivially find nothing uncited"). Everything downstream of the spans is the
**real production path**: the real ``SourceRegistry``, the real ``parse_citations``, the
real ``verify_turn``, the real ``build_grounding_record``, and the real
``is_unconfounded_observation``.

**Why it needs no model.** ``verify_turn`` is pure and synchronous, and the corpus
deliberately excludes the entity-free predicate class that escalates to a live entailment
judge. So the derivation is deterministic and AC-1's tolerance is *zero* disagreement:
there is no sampling noise to absorb, and a divergence is a defect rather than variance.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from personal_agent.captains_log.turn_evidence import GroundingRecord
from personal_agent.grounding.citations import parse_citations
from personal_agent.grounding.compliance import is_unconfounded_observation
from personal_agent.grounding.source_registry import IDENTIFIER_DIGEST_CHARS, SourceRegistry
from personal_agent.grounding.spans import (
    ExemptRegion,
    NonExemptReason,
    Span,
    SpanExtraction,
    SpanLabel,
    assert_non_overlapping,
)
from personal_agent.grounding.verification import build_grounding_record, verify_turn

from .corpus import UNRESOLVED_REF, LabelledSource, LabelledTurn, SourceKind

PLACEHOLDER = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
"""``{{S1}}`` — rewritten to the real minted marker before anything is measured."""

UNRESOLVED_MARKER = f"[S99@{'0' * IDENTIFIER_DIGEST_CHARS}]"
"""A well-formed identifier that no registry mints, for the D3(a) failure case."""


class TurnScore(BaseModel):
    """One turn scored both ways.

    Attributes:
        doc_id: Which turn.
        labelled_in_denominator: What the labeller said.
        measured_in_denominator: What ``is_unconfounded_observation`` decided.
        labelled_compliant: The hand label, or None outside the denominator.
        measured_compliant: What the real pipeline decided, or None when the turn is not
            an observation.
        record: The grounding record the measurement came from, for diagnosis.
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    labelled_in_denominator: bool
    measured_in_denominator: bool
    labelled_compliant: bool | None
    measured_compliant: bool | None
    record: GroundingRecord

    @property
    def agrees(self) -> bool:
        """Whether both scorings reached the same verdict on this turn."""
        return (
            self.labelled_in_denominator == self.measured_in_denominator
            and self.labelled_compliant == self.measured_compliant
        )

    @property
    def disagreement(self) -> str:
        """A one-line description of how the two scorings differ."""
        if self.agrees:
            return ""
        if self.labelled_in_denominator != self.measured_in_denominator:
            return (
                f"{self.doc_id}: denominator labelled {self.labelled_in_denominator}, "
                f"measured {self.measured_in_denominator}"
            )
        outcomes = ", ".join(f"{s.text[:40]!r}={s.outcome}" for s in self.record.spans)
        return (
            f"{self.doc_id}: compliance labelled {self.labelled_compliant}, "
            f"measured {self.measured_compliant} [{outcomes}]"
        )


def _register(registry: SourceRegistry, source: LabelledSource) -> str | None:
    """Register one labelled source and return its minted identifier.

    Args:
        registry: The turn's registry.
        source: The labelled source.

    Returns:
        The identifier, or None when the registry declined to register it.
    """
    if source.kind is SourceKind.MEMORY:
        item: dict[str, object] = {
            "description": source.content,
            "identity": source.identity,
        }
        if source.asserted_by is not None:
            item["asserted_by"] = source.asserted_by
        return registry.register_memory_item(item).identifier

    registration = registry.register_tool_result(
        tool_name=source.tool or "",
        arguments=dict(source.arguments),
        content=source.content,
    )
    return registration.source.identifier if registration.source else None


def render(turn: LabelledTurn) -> tuple[str, SourceRegistry]:
    """Build the turn's real registry and substitute its citation placeholders.

    Identifiers are content- and turn-bound, so they cannot be written into the corpus by
    hand — the registry mints them here and the placeholders are rewritten to match.

    Args:
        turn: The labelled turn.

    Returns:
        The reply with real citation markers, and the registry that minted them.

    Raises:
        ValueError: When a placeholder names a source the document does not declare, or
            one the registry declined to register.
    """
    registry = SourceRegistry(turn_id=turn.doc_id)
    identifiers: dict[str, str] = {}
    for source in turn.sources:
        identifier = _register(registry, source)
        if identifier is None:
            raise ValueError(f"{turn.doc_id}: source {source.ref} was not registered")
        identifiers[source.ref] = identifier

    def substitute(match: re.Match[str]) -> str:
        ref = match.group(1)
        if ref == UNRESOLVED_REF:
            return UNRESOLVED_MARKER
        if ref not in identifiers:
            raise ValueError(f"{turn.doc_id}: placeholder {{{{{ref}}}}} names no declared source")
        return f"[{identifiers[ref]}]"

    return PLACEHOLDER.sub(substitute, turn.text).strip(), registry


def extraction_from_labels(turn: LabelledTurn, text: str) -> SpanExtraction:
    """Build a ``SpanExtraction`` from the corpus's independent labelling.

    This is the substitution AC-1 requires: the spans are the labeller's, so what is
    measured downstream is the *metric*, not the extractor that FRE-1281 already measured.

    Args:
        turn: The labelled turn.
        text: The citation-substituted reply.

    Returns:
        The extraction.

    Raises:
        ValueError: When a labelled quote cannot be anchored, or the labels overlap.
    """
    from .corpus import locate  # noqa: PLC0415

    spans: list[Span] = []
    for labelled in turn.spans:
        start, end = locate(text, labelled.text)
        label = SpanLabel(labelled.label)
        spans.append(
            Span(
                start=start,
                end=end,
                text=labelled.text,
                label=label,
                region=ExemptRegion(labelled.region) if labelled.region else None,
                reason=(
                    NonExemptReason.CLASSIFIED if label is SpanLabel.CLAIM_NON_EXEMPT else None
                ),
            )
        )
    assert_non_overlapping(spans)
    return SpanExtraction(output=text, spans=tuple(sorted(spans, key=lambda s: s.start)))


def score(turn: LabelledTurn) -> TurnScore:
    """Score one labelled turn through the real verification and metric path.

    Args:
        turn: The labelled turn.

    Returns:
        Both verdicts, side by side.
    """
    text, registry = render(turn)
    extraction = extraction_from_labels(turn, text)
    verification = verify_turn(extraction, parse_citations(text), registry)
    record = build_grounding_record(
        verification, mode="observe", attempts=1, retrieval_forced=False
    )

    in_denominator = is_unconfounded_observation(record)
    return TurnScore(
        doc_id=turn.doc_id,
        labelled_in_denominator=turn.denominator,
        measured_in_denominator=in_denominator,
        labelled_compliant=turn.compliant,
        measured_compliant=record.first_generation_compliant if in_denominator else None,
        record=record,
    )


def score_all(turns: Sequence[LabelledTurn]) -> tuple[TurnScore, ...]:
    """Score every turn in a partition.

    Args:
        turns: The labelled turns.

    Returns:
        One score per turn, in order.
    """
    return tuple(score(turn) for turn in turns)


def disagreements(scores: Sequence[TurnScore]) -> tuple[str, ...]:
    """Return a readable line per disagreeing turn.

    Args:
        scores: Scored turns.

    Returns:
        The disagreements, empty when the two scorings agree everywhere.
    """
    return tuple(item.disagreement for item in scores if not item.agrees)


__all__ = [
    "PLACEHOLDER",
    "UNRESOLVED_MARKER",
    "TurnScore",
    "disagreements",
    "extraction_from_labels",
    "render",
    "score",
    "score_all",
]
