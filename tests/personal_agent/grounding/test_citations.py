"""Citation format — one marker binds one span, resolution is turn-scoped (FRE-1280).

ADR-0138 D1 (explicit binding) and D3(a) (resolution against *this* turn's registry).
"""

from __future__ import annotations

from personal_agent.grounding.citations import (
    CitationViolationKind,
    parse_citations,
    resolve_citations,
)
from personal_agent.grounding.source_registry import SourceRegistry

TURN_A = "trace-aaaa-1111"
TURN_B = "trace-bbbb-2222"


def _registry_with_two_sources(turn_id: str) -> SourceRegistry:
    """A registry holding two tool sources, so markers have something to resolve to."""
    registry = SourceRegistry(turn_id=turn_id)
    registry.register_tool_result(
        tool_name="mcp_fetch_content",
        arguments={"url": "https://example.com/ortiz"},
        content="Ortiz packs bonito del norte in olive oil.",
    )
    registry.register_tool_result(
        tool_name="mcp_fetch_content",
        arguments={"url": "https://example.com/nardin"},
        content="Nardin packs bonito in a Basque cannery.",
    )
    return registry


# ── AC-4 — one marker binds one span, unambiguously ─────────────────────────────


def test_adjacent_assertions_bind_separately() -> None:
    """D1's own example: each assertion carries its own adjacent marker.

    The span *text* is asserted, not only the identifier — binding the whole sentence to
    the last marker would still yield two spans with two identifiers and pass an
    identifier-only check.
    """
    registry = _registry_with_two_sources(TURN_A)
    first, second = registry.sources()

    text = f"Ortiz [{first.identifier}] is better than Nardin [{second.identifier}]"
    parse = parse_citations(text)

    assert [(span.text, span.identifier) for span in parse.spans] == [
        ("Ortiz", first.identifier),
        ("is better than Nardin", second.identifier),
    ]
    assert parse.violations == ()
    assert parse.uncited_regions == ()

    resolutions = resolve_citations(parse, registry)
    assert [resolution.source for resolution in resolutions] == [first, second]
    assert all(resolution.resolved for resolution in resolutions)


def test_multiply_bound_span_flagged() -> None:
    """Two markers with no text between them bind nothing of their own."""
    registry = _registry_with_two_sources(TURN_A)
    first, second = registry.sources()

    parse = parse_citations(f"Ortiz packs bonito [{first.identifier}][{second.identifier}]")

    assert [violation.kind for violation in parse.violations] == [
        CitationViolationKind.MULTIPLY_BOUND
    ]
    assert parse.violations[0].identifier == second.identifier
    # The first marker still bound its span; only the second one is the violation.
    assert [span.text for span in parse.spans] == ["Ortiz packs bonito"]


def test_uncited_region_reported_not_judged() -> None:
    """Text after the last marker is reported as uncited, not as a violation.

    Whether an uncited region *needed* a citation is span extraction's call (FRE-1281),
    so this ticket states only what the format shows.
    """
    registry = _registry_with_two_sources(TURN_A)
    first, _ = registry.sources()

    parse = parse_citations(f"Ortiz packs bonito [{first.identifier}]. Tuna is cheap in Brittany.")

    assert [span.text for span in parse.spans] == ["Ortiz packs bonito"]
    assert [region.text for region in parse.uncited_regions] == [". Tuna is cheap in Brittany."]
    assert parse.violations == ()


def test_text_with_no_markers_is_one_uncited_region() -> None:
    """The uncited case the whole contract exists to catch, stated without judging it."""
    parse = parse_citations("Ortiz is better than Nardin.")

    assert parse.spans == ()
    assert [region.text for region in parse.uncited_regions] == ["Ortiz is better than Nardin."]


def test_trailing_punctuation_is_not_an_uncited_region() -> None:
    """A full stop after the final marker is not uncited text."""
    registry = _registry_with_two_sources(TURN_A)
    first, _ = registry.sources()

    parse = parse_citations(f"Ortiz packs bonito [{first.identifier}].")

    assert parse.uncited_regions == ()
    assert parse.violations == ()


def test_multi_claim_region_binds_as_one_region() -> None:
    """The stated FRE-1281 boundary, pinned rather than left to be discovered.

    ``Paris is France's capital and has 2.1 million residents [S1]`` is two atomic
    propositions under D1 and one bound region under this parser. The parse is
    deterministic, not ambiguous — it is *under-segmented*, and segmenting into atomic
    claims is the span extractor D1 requires to be "a named component, not a regex".
    """
    registry = _registry_with_two_sources(TURN_A)
    first, _ = registry.sources()

    parse = parse_citations(
        f"Paris is France's capital and has 2.1 million residents [{first.identifier}]"
    )

    assert len(parse.spans) == 1
    assert parse.spans[0].text == "Paris is France's capital and has 2.1 million residents"


def test_span_offsets_locate_the_bound_text() -> None:
    """Offsets are what a downstream extractor re-segments against."""
    registry = _registry_with_two_sources(TURN_A)
    first, _ = registry.sources()

    text = f"Ortiz packs bonito [{first.identifier}]"
    parse = parse_citations(text)

    span = parse.spans[0]
    assert text[span.start : span.end] == span.text


def test_malformed_markers_are_not_markers() -> None:
    """Bracketed text that is not a well-formed identifier is ordinary prose."""
    parse = parse_citations("Ortiz [S1] is better than Nardin [S2@short] or [not-an-id]")

    assert parse.spans == ()
    assert len(parse.uncited_regions) == 1


# ── AC-5 — resolution is turn-scoped ────────────────────────────────────────────


def test_previous_turn_marker_does_not_resolve() -> None:
    """A marker minted last turn parses fine and resolves to nothing (D3(a))."""
    stale = _registry_with_two_sources(TURN_A).sources()[0]
    this_turn = _registry_with_two_sources(TURN_B)
    own = this_turn.sources()[0]

    parse = parse_citations(f"Ortiz packs bonito [{stale.identifier}]")
    resolutions = resolve_citations(parse, this_turn)

    assert len(resolutions) == 1
    assert resolutions[0].resolved is False
    assert resolutions[0].source is None

    # Paired positive: this turn's own marker resolves, so "resolve nothing" fails too.
    own_parse = parse_citations(f"Ortiz packs bonito [{own.identifier}]")
    assert resolve_citations(own_parse, this_turn)[0].source is own
