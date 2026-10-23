"""A primary ``web_search`` gives each result entry its own source (FRE-1662).

FRE-1280 registers a primary ``web_search`` call as one source with no referent, so a
citation of it can never become a link. Each entry that carries a ``url`` now also
registers on its own, with that ``url`` as its referent, under the same ADR-0138 D2 rules
as the sub-agent path (FRE-442).

Each positive control here is paired with its seeded negative, per ADR-0138's testing
strategy.
"""

from __future__ import annotations

import json

from personal_agent.grounding.source_registry import (
    Admissibility,
    Entitlement,
    SourceKind,
    SourceRegistry,
    ToolRegistration,
    url_entries,
)

TURN = "trace-primary-entries"
KAYAK = "https://www.kayak.com/flights/PAR-NYC"
EXPEDIA = "https://www.expedia.com/deals/paris"
BUSINESS = "https://businessclasstravel.us/paris-new-york"
QUERY = "Paris New York business class price"


def _search_output() -> dict[str, object]:
    """One ``web_search`` result, in the contract shape ``render_content`` emits."""
    return {
        "ok": True,
        "answers": [],
        "results": [
            {"title": "Flights", "url": KAYAK, "snippet": "From EUR 2,410.", "engine": "brave"},
            {"title": "Deals", "url": EXPEDIA, "snippet": "From EUR 1,980.", "engine": "exa"},
            {"title": "Guide", "url": BUSINESS, "snippet": "EUR 3,105.", "engine": "brave"},
        ],
        "result_count": 3,
        "query": QUERY,
    }


def _register_entries(
    registry: SourceRegistry, output: dict[str, object], arguments: dict[str, object]
) -> list[ToolRegistration]:
    """Offer every url-bearing entry of ``output``, as the executor does."""
    return [
        registry.register_result_entry(tool_name="web_search", arguments=arguments, entry=entry)
        for entry in url_entries(output)
    ]


# ── AC-1: entries register ──────────────────────────────────────────────────────────────


def test_three_entries_register_three_sources_with_their_urls() -> None:
    """The positive control: one source per entry, each with its own url as referent."""
    registry = SourceRegistry(turn_id=TURN)

    registrations = _register_entries(registry, _search_output(), {"query": QUERY})

    sources = registry.sources()
    assert len(sources) == 3
    assert [s.referent for s in sources] == [KAYAK, EXPEDIA, BUSINESS]
    assert all(s.kind is SourceKind.TOOL and s.origin == "web_search" for s in sources)
    assert all(s.entitlement is Entitlement.EXTERNAL for s in sources)
    assert all(r.admissibility is Admissibility.ADMISSIBLE for r in registrations)
    # The source is the entry the model read, not the whole result set.
    assert "EUR 1,980" in sources[1].content
    assert "EUR 2,410" not in sources[1].content
    # One offer per entry, so admitted never exceeds offered (ADR-0139 D1).
    assert registry.tool_results_offered == 3
    assert registry.tool_results_admitted == 3


def test_the_entry_identifiers_are_distinct_and_resolve() -> None:
    registry = SourceRegistry(turn_id=TURN)

    _register_entries(registry, _search_output(), {"query": QUERY})

    identifiers = [s.identifier for s in registry.sources()]
    assert len(set(identifiers)) == 3
    assert all(registry.resolve(i) is not None for i in identifiers)


def test_url_entries_returns_only_objects_with_a_url() -> None:
    output = _search_output()
    output["answers"] = [{"engine": "openmeteo", "type": "weather", "summary": "Sunny"}]
    output["infoboxes"] = [{"title": "Paris", "urls": [KAYAK]}]
    results = output["results"]
    assert isinstance(results, list)
    results.append({"title": "No link", "url": "", "snippet": "EUR 900."})

    assert [entry["url"] for entry in url_entries(output)] == [KAYAK, EXPEDIA, BUSINESS]


# ── AC-2: no invented source ────────────────────────────────────────────────────────────


def test_an_entry_with_no_url_registers_nothing() -> None:
    """The seeded negative: an entry without a url has nothing to link to."""
    registry = SourceRegistry(turn_id=TURN)

    registration = registry.register_result_entry(
        tool_name="web_search",
        arguments={"query": QUERY},
        entry={"title": "No link", "url": "", "snippet": "EUR 900."},
    )

    assert registration.source is None
    assert registration.admissibility is Admissibility.NO_CONTENT
    assert registry.sources() == ()


def test_a_url_only_in_the_arguments_registers_nothing() -> None:
    """The seeded negative: a url the model wrote is not a source (ADR-0138 D2).

    The model put a url in its query, and the result echoes the query back. A snippet also
    names a url as text. Neither is a ``url`` field of a result entry, so only the entry the
    search really returned registers.
    """
    planted = "https://invented.example/paris-fares"
    output = {
        "ok": True,
        "results": [
            {"title": "Flights", "url": KAYAK, "snippet": f"See also {planted} for fares."}
        ],
        "query": planted,
    }
    registry = SourceRegistry(turn_id=TURN)

    _register_entries(registry, output, {"query": planted})

    assert [s.referent for s in registry.sources()] == [KAYAK]
    assert registry.tool_results_offered == 1


def test_a_tainted_search_registers_no_entries() -> None:
    """A search that addresses state an inadmissible call wrote this turn (ADR-0138 D2)."""
    registry = SourceRegistry(turn_id=TURN)
    registry.register_tool_result(
        tool_name="bash", arguments={"command": QUERY}, content="written by the model"
    )

    registrations = _register_entries(registry, _search_output(), {"query": QUERY})

    assert all(r.source is None for r in registrations)
    assert all(r.admissibility is Admissibility.DERIVED_FROM_TURN_WRITE for r in registrations)


def test_a_url_field_in_a_non_search_result_registers_nothing() -> None:
    """A file or a page that lists a url did not retrieve that url (FRE-442 security fix)."""
    registry = SourceRegistry(turn_id=TURN)

    registration = registry.register_result_entry(
        tool_name="read",
        arguments={"path": "/notes/links.json"},
        entry={"url": KAYAK, "note": "cheap fares"},
    )

    assert registration.source is None
    assert registration.admissibility is Admissibility.UNCLASSIFIED_TOOL
    assert registry.refused_origin_admissibility == ("read:unclassified_tool",)
