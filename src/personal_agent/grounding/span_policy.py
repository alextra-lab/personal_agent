"""Layer 3 — D1's invariants, enforced deterministically (ADR-0138 D1, FRE-1281).

Layer 3 runs after the model pass and **only ever moves a label toward**
:attr:`~personal_agent.grounding.spans.SpanLabel.CLAIM_NON_EXEMPT`. The asymmetry is the
safety argument: this layer can manufacture a false positive, which the precision bar
measures and which costs usability, but it can never rescue a claim from the contract.
Every rule below is therefore a tightening.

**Coverage is conserved, and a gap fails closed.** A plan review found the original
design governing only the spans layer 2 chose to return, which meant a claim layer 2
simply *omitted* fell through with no record at all — "the default is deny" undercut by
silence rather than by a decision. Layer 2 now tiles each region it is given, and any
character it left uncovered becomes a
:attr:`~personal_agent.grounding.spans.NonExemptReason.COVERAGE_GAP` span. A malfunction
costs precision, never recall.

**Where layer 3 stops, deliberately.** It does not convert
:attr:`~personal_agent.grounding.spans.SpanLabel.NOT_A_CLAIM` into a claim. A span layer
2 examined and judged inert stays inert, because deciding claim-hood is layer 2's job and
a wrongly-inert span is already a recall miss the corpus measures. Overriding it here
would make layer 3 a second, unmeasured classifier — the exact thing D1 says a regex
cannot be. The one exception is a coverage gap, where layer 2 made no judgement to
respect.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from personal_agent.grounding.code_regions import Region, RegionKind
from personal_agent.grounding.spans import (
    ExemptRegion,
    NonExemptReason,
    Span,
    SpanExtraction,
    SpanLabel,
)

CHECKABLE_PREDICATES: frozenset[str] = frozenset(
    {
        "well regarded",
        "well-regarded",
        "safe",
        "popular",
        "recommended",
        "reliable",
    }
)
"""Predicates D1 names as claims however evaluative they sound.

ADR-0138 round 3 closed this leak: "An earlier draft used *'are both well regarded'* as
the exemplar of exempt evaluation; that was wrong, and it was the common-knowledge trap
reappearing one level down." Each of these is externally checkable — someone could
establish whether it holds — so none of them may ride out on the connective-evaluative
exemption. Applied only to spans the model tried to mark **exempt**: that is the
laundering channel, and widening it to inert text would sweep in ordinary questions.
"""

_PREDICATE_PATTERN = re.compile(
    r"\b(?:"
    + "|".join(sorted((re.escape(p) for p in CHECKABLE_PREDICATES), key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)

_NON_WHITESPACE = re.compile(r"\S")


def _carries_checkable_predicate(text: str) -> bool:
    """Whether a span smuggles an externally checkable predicate."""
    return bool(_PREDICATE_PATTERN.search(text))


def _pin(span: Span, reason: NonExemptReason) -> Span:
    """Rewrite a span as non-exempt, dropping any exemption annotation."""
    return Span(
        start=span.start,
        end=span.end,
        text=span.text,
        label=SpanLabel.CLAIM_NON_EXEMPT,
        reason=reason,
    )


def _tighten(span: Span) -> Span:
    """Apply the categorical pins to one model-produced span.

    Args:
        span: A span as layer 2 returned it.

    Returns:
        The span, possibly re-labelled non-exempt.
    """
    if span.label is not SpanLabel.CLAIM_EXEMPT:
        return span
    if span.region is ExemptRegion.AMBIGUOUS:
        return _pin(span, NonExemptReason.AMBIGUITY_PIN)
    if _carries_checkable_predicate(span.text):
        return _pin(span, NonExemptReason.CHECKABLE_PREDICATE_PIN)
    return span


def _resolve_overlaps(spans: Sequence[Span]) -> list[Span]:
    """Enforce D1's one-directional precedence: non-exempt wins.

    An overlapping exempt span is **dropped, not trimmed** — trimming it around the claim
    would emit a fragment of a proposition, and D1's spans are atomic.

    Args:
        spans: Spans in any order, possibly overlapping.

    Returns:
        Non-overlapping spans in document order.
    """
    # Non-exempt first so it claims the ground; then longest, so a precise short claim
    # does not lose to an accidental long one at the same offset.
    ordered = sorted(
        spans,
        key=lambda s: (
            s.label is not SpanLabel.CLAIM_NON_EXEMPT,
            s.start,
            -(s.end - s.start),
        ),
    )
    kept: list[Span] = []
    for span in ordered:
        if any(span.overlaps(existing) for existing in kept):
            continue
        kept.append(span)
    return sorted(kept, key=lambda s: s.start)


def _fill_gaps(output: str, region: Region, covered: Sequence[Span]) -> tuple[list[Span], bool]:
    """Emit spans for characters of ``region`` no layer-2 span covered.

    Args:
        output: The whole model output.
        region: A region layer 2 was asked to tile.
        covered: Layer-2 spans falling inside ``region``.

    Returns:
        ``(gap_spans, degraded)`` — ``degraded`` is set when a gap held real text.
    """
    gaps: list[Span] = []
    degraded = False
    cursor = region.start
    boundaries = sorted(covered, key=lambda s: s.start)

    def emit(start: int, end: int) -> None:
        nonlocal degraded
        if end <= start:
            return
        text = output[start:end]
        if not _NON_WHITESPACE.search(text):
            # Whitespace between segments is not an omission — failing closed on it
            # would make every inter-segment space a citation obligation.
            gaps.append(Span(start=start, end=end, text=text, label=SpanLabel.NOT_A_CLAIM))
            return
        degraded = True
        gaps.append(
            Span(
                start=start,
                end=end,
                text=text,
                label=SpanLabel.CLAIM_NON_EXEMPT,
                reason=NonExemptReason.COVERAGE_GAP,
            )
        )

    for span in boundaries:
        emit(cursor, span.start)
        cursor = max(cursor, span.end)
    emit(cursor, region.end)
    return gaps, degraded


def apply_policy(
    output: str,
    regions: Sequence[Region],
    classified: Sequence[Span],
) -> SpanExtraction:
    """Combine layer 1's verdicts with layer 2's tiling under D1's invariants.

    Args:
        output: The model output being classified.
        regions: Layer 1's partition of ``output``.
        classified: Layer 2's spans. Only those falling inside a
            :attr:`~personal_agent.grounding.code_regions.RegionKind.CLASSIFY` region are
            consulted; layer 2 has no say over proven code or dependency declarations.

    Returns:
        Non-overlapping spans in document order, tiling ``output``, with ``degraded`` set
        if any rule had to fail closed.
    """
    spans: list[Span] = []
    degraded = False

    for region in regions:
        if region.kind is RegionKind.PROVEN_CODE:
            spans.append(
                Span(
                    start=region.start,
                    end=region.end,
                    text=region.text,
                    label=SpanLabel.CLAIM_EXEMPT,
                    region=ExemptRegion.CODE,
                )
            )
            continue
        if region.kind is RegionKind.DEPENDENCY:
            spans.append(
                Span(
                    start=region.start,
                    end=region.end,
                    text=region.text,
                    label=SpanLabel.CLAIM_NON_EXEMPT,
                    reason=NonExemptReason.DEPENDENCY_PIN,
                )
            )
            continue
        if region.kind is RegionKind.STRUCTURAL:
            spans.append(
                Span(
                    start=region.start,
                    end=region.end,
                    text=region.text,
                    label=SpanLabel.NOT_A_CLAIM,
                )
            )
            continue

        inside = [
            _tighten(span)
            for span in classified
            if span.start >= region.start and span.end <= region.end
        ]
        inside = _resolve_overlaps(inside)
        gaps, region_degraded = _fill_gaps(output, region, inside)
        degraded = degraded or region_degraded
        spans.extend(inside)
        spans.extend(gaps)

    resolved = _resolve_overlaps(spans)
    return SpanExtraction(output=output, spans=tuple(resolved), degraded=degraded)


__all__ = ["CHECKABLE_PREDICATES", "apply_policy"]
