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

_MARKER_WITH_LEADING_SPACE = re.compile(
    rf"[ \t]?\[(?:S\d+@[0-9a-f]{{{IDENTIFIER_DIGEST_CHARS}}})\]"
)
"""A marker together with the one space that introduced it.

Taking the space with the marker is what lets ``residents [S1@…].`` become ``residents.``
without any global whitespace pass — see :func:`strip_citation_markers` for why a global
pass is not an option.
"""


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


def strip_citation_markers(text: str) -> str:
    r"""Remove every citation marker from text, leaving the prose intact.

    A marker is an artifact of the **verification protocol**, not content. Once
    verification has consumed it, carrying it further is pure leakage — and the leak has
    two mouths, both of which this closes at one point (FRE-1282):

    - **The reader.** FRE-1283 instructs the model to emit markers and FRE-1296 renders
      real identifiers for it to copy, so without a strip the user receives raw
      ``[S1@a3f91c2b7d4e6f80]`` in the reply.
    - **Storage, and through it recall.** ``captains_log/capture.py`` persists the
      delivered reply as ``assistant_response``, and entity extraction reads captures. A
      marker surviving into an entity description would be re-injected by a later turn's
      recall, where — identifiers being turn-scoped by construction (D3(a)) — it would
      resolve to nothing and manufacture a D4 refusal on a turn that did nothing wrong.

    **The repair is local to each marker, and that is a correctness requirement rather
    than a nicety.** A first version tidied globally — collapse runs of spaces, strip each
    line's trailing whitespace — and a security review reproduced what that does to a reply
    containing code::

        "def f():\n    if x:\n        return 1"  ->  "def f():\n if x:\n return 1"

    Every indented code block, nested list and two-space hard break in every reply, in
    every mode, since this runs unconditionally. Whitespace is content in Markdown, so the
    only safe edit is the one the marker itself made necessary: the single space that used
    to separate the prose from the marker goes with it, and nothing else is touched.

    Args:
        text: Model output, possibly carrying markers.

    Returns:
        The same text with every well-formed marker removed. Bracketed text that is not a
        well-formed identifier is ordinary prose and is left alone, for the same reason
        :data:`CITATION_MARKER_PATTERN` does not match it.
    """
    return _MARKER_WITH_LEADING_SPACE.sub("", text)


_NEAR_MISS_CANDIDATE_PATTERN = re.compile(r"\[[^\]]*@[^\]]*\]")
"""A bracketed, ``@``-bearing span — the shape a citation marker takes when malformed.

Only ``]`` is excluded from the character classes, not ``[``. Excluding both would let a
malformed marker carrying a nested ``[`` before its digest (``[S1@[0123]]``) match
nothing at all, since the class could not cross the inner bracket either — the exact gap
an ADR-0139 D1 plan review caught. Excluding only ``]`` still bounds each candidate to
"the next ``]`` after this ``[``", so two adjacent well-formed markers are scanned as two
independent candidates rather than conflated into one run.
"""


def count_near_miss_markers(text: str) -> int:
    """Count citation-shaped strings that fail the well-formed marker pattern.

    ADR-0139 D1's near-miss signal: a candidate that is citation-shaped — bracketed,
    containing ``@`` — but does not match :data:`CITATION_MARKER_PATTERN`. FRE-1327's
    ``[S@bash-tempo-trace-dba5b2]`` is the worked case: it fails on ordinal, hex and
    length, so it scores as an ordinary no-source outcome today, indistinguishable from
    not trying to cite at all.

    Deliberately narrow, and deliberately does not resolve a candidate against the
    registry: resolution on registry-minted attributes only is D7's job (FRE-1355), kept
    separate so a near-miss can never be rescued into a citation by construction.

    Args:
        text: The model's output for one turn, markers intact.

    Returns:
        How many candidates failed the well-formed pattern.
    """
    return sum(
        1
        for candidate in _NEAR_MISS_CANDIDATE_PATTERN.findall(text)
        if not CITATION_MARKER_PATTERN.fullmatch(candidate)
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
