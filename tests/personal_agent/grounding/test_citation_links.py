"""A marker that resolves to a real web source becomes a link (FRE-442).

Owner decision, 2026-10-23: a marker becomes a link when it resolves in this turn's
registry to a source with a web URL. The link shows where a claim came from. It does not
certify the claim. Every other marker is stripped exactly as before.
"""

from __future__ import annotations

import json

from personal_agent.grounding.citations import CITATION_MARKER_PATTERN, link_citation_markers
from personal_agent.grounding.source_registry import RetrievedToolResult, SourceRegistry

TURN = "trace-links-0001"
PAGE = "https://www.example.com/paris"


def _registry_with(url: str) -> tuple[SourceRegistry, str]:
    """A registry holding one fetched page whose referent is ``url``."""
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": url},
        content="Paris counts 2,100,000 residents.",
    )
    assert registration.source is not None
    return registry, registration.source.identifier


def test_a_resolved_web_source_becomes_a_link_named_by_its_host() -> None:
    """The positive control: the reader gets a clickable link to the page."""
    registry, ident = _registry_with(PAGE)

    linked = link_citation_markers(f"Paris has 2.1 million residents [{ident}].", registry)

    assert linked == f"Paris has 2.1 million residents ([example.com](<{PAGE}>))."


def test_an_unresolved_marker_is_stripped_with_no_link() -> None:
    """The seeded negative: a well-formed identifier this turn never minted."""
    registry, _ = _registry_with(PAGE)
    stale = "S9@0123456789abcdef"

    linked = link_citation_markers(f"Paris has 2.1 million residents [{stale}].", registry)

    assert linked == "Paris has 2.1 million residents."


def test_a_source_with_no_referent_is_stripped_with_no_link() -> None:
    """A primary ``web_search`` source is a result set: it has no single URL to link."""
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="web_search",
        arguments={"query": "paris population"},
        content=json.dumps({"results": [{"url": PAGE, "snippet": "2.1 million"}]}),
    )
    assert registration.source is not None and registration.source.referent is None

    linked = link_citation_markers(
        f"About 2.1 million [{registration.source.identifier}].", registry
    )

    assert linked == "About 2.1 million."


def test_a_worker_finding_source_links_to_its_own_url() -> None:
    """A sub-agent finding carries its ``source_url`` as the referent."""
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_worker_finding(
        source_url=PAGE,
        retrieved=(
            RetrievedToolResult(
                tool_name="web_search",
                arguments={"query": "paris population"},
                content=json.dumps({"results": [{"url": PAGE, "snippet": "2.1 million"}]}),
            ),
        ),
    )
    assert registration.source is not None

    linked = link_citation_markers(
        f"About 2.1 million [{registration.source.identifier}].", registry
    )

    assert linked == f"About 2.1 million ([example.com](<{PAGE}>))."


def test_no_raw_marker_survives_linking() -> None:
    """Linked or stripped, the output carries no marker at all."""
    registry, ident = _registry_with(PAGE)
    text = f"A [{ident}]. B [S2@fedcba9876543210]. C [{ident}]."

    linked = link_citation_markers(text, registry)

    assert CITATION_MARKER_PATTERN.search(linked) is None
    assert linked.count(f"(<{PAGE}>)") == 2


def test_a_non_web_scheme_is_never_linked() -> None:
    """Only http and https become links: ``javascript:`` and ``file:`` are stripped."""
    for url in ("javascript:alert(1)", "file:///etc/passwd", "ftp://example.com/x"):
        registry, ident = _registry_with(url)

        linked = link_citation_markers(f"Claim [{ident}].", registry)

        assert linked == "Claim.", url


def test_a_url_that_could_break_the_markdown_is_not_linked() -> None:
    """Unsafe destinations, credentials and non-DNS hosts are stripped, never linked."""
    for url in (
        "https://example.com/a b",
        "https://example.com/<x>",
        "https://exa mple.com/",
        "https://user:secret@example.com/x",
        "https://[::1]/x",
    ):
        registry, ident = _registry_with(url)

        linked = link_citation_markers(f"Claim [{ident}].", registry)

        assert linked == "Claim.", url


def test_a_pipe_in_the_url_is_encoded_so_a_table_cell_survives() -> None:
    """GFM splits a table cell on ``|`` even inside a link destination."""
    url = "https://example.com/search?q=a|b"
    registry, ident = _registry_with(url)

    linked = link_citation_markers(f"| fare | [{ident}] |", registry)

    assert "|b" not in linked
    assert "https://example.com/search?q=a%7Cb" in linked
    assert linked.count("|") == 3


def test_code_indentation_is_untouched() -> None:
    """The repair stays local to each marker, as ``strip_citation_markers`` requires."""
    registry, ident = _registry_with(PAGE)
    code = "def f():\n    if x:\n        return 1"

    linked = link_citation_markers(f"{code}\n\nPopulation [{ident}].", registry)

    assert linked.startswith(code)
