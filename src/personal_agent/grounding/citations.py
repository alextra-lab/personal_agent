"""Citation output format and turn-scoped resolution (ADR-0138 D1 and D3(a), FRE-1280).

ADR-0138 D1 requires that citation binding be **explicit by construction**: each
non-exempt span carries its own adjacent marker, and binding is never inferred from
proximity, clause or sentence boundaries. ``Ortiz [S1] is better than Nardin [S2]``,
never ``Ortiz is better than Nardin [S1]`` leaving it ambiguous which source covers what.

The binding rule, stated once so nothing downstream has to infer it:

    **A marker binds the contiguous text from the end of the previous marker (or the
    start of the text) up to its own opening bracket, whitespace trimmed.**

**What this module deliberately does not do.** It reports the binding the *format*
expresses. It does not decide whether a region of text was an assertion requiring a
citation — that is span extraction, which D1 requires to be "a named component, not a
regex" with measured recall and precision (FRE-1281, AC-7). So unmarked text is reported
as a neutral :class:`UncitedRegion` rather than as a violation: an earlier draft called it
an ``UNBOUND_SPAN``, which asserts a classification this module cannot perform.

For the same reason ``Paris is France's capital and has 2.1 million residents [S1]``
parses as **one** bound region. That parse is deterministic rather than ambiguous, but it
is *under-segmented* — two atomic propositions share one marker. Segmentation into atomic
claims is the extractor's job, and it runs before this format check in the finished
pipeline.

Resolution against the turn's registry lives here because D3(a) is this ticket's
requirement; deciding what to *do* about a citation that fails to resolve — block, retry,
refuse — is D4 and belongs to FRE-1282. Hence :func:`resolve_citations` reports an
outcome and never raises or blocks.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from personal_agent.grounding.source_registry import (
    IDENTIFIER_DIGEST_CHARS,
    RegisteredSource,
    SourceRegistry,
)

CITATION_MARKER_PATTERN = re.compile(rf"\[(S\d+@[0-9a-f]{{{IDENTIFIER_DIGEST_CHARS}}})\]")
"""The one citation marker form the contract recognises — ``[S1@a3f91c2b7d]``.

Built from :data:`~personal_agent.grounding.source_registry.IDENTIFIER_DIGEST_CHARS` so
the format and the registry that mints identifiers cannot drift apart. Bracketed text that
is not a well-formed identifier is ordinary prose, not a malformed citation: treating
``[see below]`` as a broken marker would manufacture violations out of normal writing.
"""

_WORD_PATTERN = re.compile(r"\w")


class CitationViolationKind(StrEnum):
    """Format violations decidable from the output alone.

    Only one member today, and deliberately so: every other candidate — "this span needed
    a citation", "this source does not support the claim" — requires a classifier or a
    fetch, and belongs to FRE-1281 and FRE-1282 respectively. A violation here is one that
    the *format* proves.
    """

    MULTIPLY_BOUND = "multiply_bound"


class CitedSpan(BaseModel):
    """One region of text bound to one citation identifier.

    Attributes:
        text: The bound text, whitespace-trimmed.
        identifier: The identifier from the marker that binds it, without brackets.
        start: Offset of ``text`` in the parsed output.
        end: End offset, exclusive, so ``output[start:end] == text``.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    identifier: str
    start: int
    end: int


class UncitedRegion(BaseModel):
    """Text carrying no citation marker — an observation, not a verdict.

    Whether the region *required* a citation is span extraction's call (FRE-1281). This
    states only what the format shows.

    Attributes:
        text: The unmarked text, whitespace-trimmed.
        start: Offset in the parsed output.
        end: End offset, exclusive.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    start: int
    end: int


class CitationViolation(BaseModel):
    """One format violation, with the marker that caused it.

    Attributes:
        kind: Which violation.
        identifier: The offending marker's identifier.
        start: Offset of the marker in the parsed output.
        end: End offset, exclusive.
    """

    model_config = ConfigDict(frozen=True)

    kind: CitationViolationKind
    identifier: str
    start: int
    end: int


class CitationParse(BaseModel):
    """What the citation format expresses about one piece of model output.

    Attributes:
        spans: Every marker-bound region, in output order.
        uncited_regions: Every region carrying no marker, in output order.
        violations: Format violations proved by the output itself.
    """

    model_config = ConfigDict(frozen=True)

    spans: tuple[CitedSpan, ...] = ()
    uncited_regions: tuple[UncitedRegion, ...] = ()
    violations: tuple[CitationViolation, ...] = ()


class SpanResolution(BaseModel):
    """One span's citation resolved against this turn's registry (D3(a)).

    Attributes:
        span: The cited span.
        source: The source it resolves to, or None when this turn registered none under
            that identifier — a stale identifier from a previous turn, or an invented one.
    """

    model_config = ConfigDict(frozen=True)

    span: CitedSpan
    source: RegisteredSource | None

    @property
    def resolved(self) -> bool:
        """Whether the citation resolved to a source present in this turn's registry."""
        return self.source is not None


def _carries_words(text: str) -> bool:
    """Whether ``text`` holds any word character.

    Punctuation and whitespace between a marker and the next one — a full stop closing the
    sentence the marker just cited — is not content and must not be reported as either an
    uncited region or a multiply-bound span.

    Args:
        text: Candidate region.

    Returns:
        True when at least one word character is present.
    """
    return _WORD_PATTERN.search(text) is not None


def parse_citations(text: str) -> CitationParse:
    """Parse citation markers out of model output and report what they bind.

    Args:
        text: The model's output for one turn.

    Returns:
        The spans each marker binds, the regions no marker binds, and any format
        violation the output itself proves.
    """
    spans: list[CitedSpan] = []
    uncited: list[UncitedRegion] = []
    violations: list[CitationViolation] = []

    cursor = 0
    for match in CITATION_MARKER_PATTERN.finditer(text):
        region = text[cursor : match.start()]
        stripped = region.strip()

        if not _carries_words(stripped):
            # Nothing of its own to bind: an adjacent second marker re-cites the previous
            # span, which D1 rules a format violation rather than a second citation.
            violations.append(
                CitationViolation(
                    kind=CitationViolationKind.MULTIPLY_BOUND,
                    identifier=match.group(1),
                    start=match.start(),
                    end=match.end(),
                )
            )
        else:
            start = cursor + (len(region) - len(region.lstrip()))
            spans.append(
                CitedSpan(
                    text=stripped,
                    identifier=match.group(1),
                    start=start,
                    end=start + len(stripped),
                )
            )
        cursor = match.end()

    tail = text[cursor:]
    stripped_tail = tail.strip()
    if _carries_words(stripped_tail):
        start = cursor + (len(tail) - len(tail.lstrip()))
        uncited.append(
            UncitedRegion(text=stripped_tail, start=start, end=start + len(stripped_tail))
        )

    return CitationParse(
        spans=tuple(spans),
        uncited_regions=tuple(uncited),
        violations=tuple(violations),
    )


def resolve_citations(parse: CitationParse, registry: SourceRegistry) -> tuple[SpanResolution, ...]:
    """Resolve each parsed span's citation against this turn's registry (D3(a)).

    Reports; never blocks. A citation that does not resolve is a fact about the turn, and
    what follows from it — block, retry with forced retrieval, or the explicit no-source
    statement — is D4 and lands with FRE-1282.

    Args:
        parse: The output of :func:`parse_citations`.
        registry: This turn's registry. Resolution is scoped to it by construction, so a
            valid identifier from a previous turn resolves to nothing.

    Returns:
        One resolution per span, in output order.
    """
    return tuple(
        SpanResolution(span=span, source=registry.resolve(span.identifier)) for span in parse.spans
    )
