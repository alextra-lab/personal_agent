"""Citation markers never survive past verification (ADR-0138 D3(a), FRE-1282).

Markers are protocol, not content. They must not reach the reader, and — because the
delivered reply is what ``capture.py`` persists and what entity extraction later reads —
they must not reach storage either, where a turn-scoped identifier would resolve to
nothing on a later turn and manufacture a refusal.
"""

from __future__ import annotations

from personal_agent.grounding.citations import (
    CITATION_MARKER_PATTERN,
    parse_citations,
    strip_citation_markers,
)
from personal_agent.grounding.source_registry import SourceRegistry

TURN = "trace-strip-0001"


def _identifier() -> str:
    """One real identifier, minted the way the turn path mints them."""
    registry = SourceRegistry(turn_id=TURN)
    registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents.",
    )
    return registry.sources()[0].identifier


def test_marker_is_removed_and_spacing_repaired() -> None:
    """The reader sees prose, not a hex identifier."""
    ident = _identifier()
    text = f"Paris has 2.1 million residents [{ident}]."

    assert strip_citation_markers(text) == "Paris has 2.1 million residents."


def test_every_marker_goes_including_adjacent_ones() -> None:
    """Two markers, including the multiply-bound shape, leave nothing behind."""
    ident = _identifier()
    text = f"Ortiz [{ident}] is better than Nardin [{ident}][{ident}]"

    stripped = strip_citation_markers(text)

    assert stripped == "Ortiz is better than Nardin"
    assert CITATION_MARKER_PATTERN.search(stripped) is None


def test_ordinary_bracketed_prose_survives() -> None:
    """``[see below]`` is writing, not a broken marker — stripping it would corrupt text."""
    text = "The guidance [see below] was revised."

    assert strip_citation_markers(text) == text


def test_stripping_a_marker_free_reply_changes_nothing_material() -> None:
    """The strip is a no-op on the overwhelmingly common shape."""
    text = "Here is a plan.\n\n- step one\n- step two"

    assert strip_citation_markers(text) == text


def test_stripped_text_carries_no_resolvable_citation() -> None:
    """The property that matters downstream: nothing left for a later turn to resolve.

    This is the outcome AC-6's storage hazard turns on, asserted directly rather than via
    the absence of a substring.
    """
    ident = _identifier()
    stripped = strip_citation_markers(f"Paris has 2.1 million residents [{ident}].")

    assert parse_citations(stripped).spans == ()


def test_indentation_and_hard_breaks_survive_the_strip() -> None:
    """Security-review finding: the repair must be local to each marker.

    A first version tidied globally — collapse space runs, rstrip each line — which
    flattened every indented code block, nested list and two-space hard break in every
    reply, in every mode, since the strip runs unconditionally. Whitespace is content in
    Markdown.
    """
    ident = _identifier()
    reply = (
        f"Here is the function [{ident}]:\n\n"
        "```python\n"
        "def f():\n"
        "    if x:\n"
        "        return 1\n"
        "```\n\n"
        "- outer\n"
        "  - nested\n"
        "line one  \n"
        "line two"
    )

    stripped = strip_citation_markers(reply)

    assert "    if x:" in stripped
    assert "        return 1" in stripped
    assert "  - nested" in stripped
    assert "line one  \n" in stripped
    assert stripped.startswith("Here is the function:")
