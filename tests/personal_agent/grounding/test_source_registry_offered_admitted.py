"""Tool-result offer/admission counts (ADR-0139 D1, AC-1 and AC-3, FRE-1332).

``uncitable_turn_rate`` needs a denominator: how many tool results this turn's registry
was offered, against how many it actually admitted. AC-3 requires the second number to
reconcile against the tool/documentation-kind source count — this file proves that
reconciliation holds by construction, including across a D4 retry's duplicate offer.
"""

from __future__ import annotations

from personal_agent.grounding.source_registry import SourceKind, SourceRegistry

TURN = "trace-offered-admitted-0001"


def test_a_refused_call_is_offered_but_not_admitted() -> None:
    """AC-1's shape: a bash call is offered, refused, and never becomes a source."""
    registry = SourceRegistry(turn_id=TURN)

    registration = registry.register_tool_result(
        tool_name="bash",
        arguments={"command": "echo 'Paris has 9 million residents'"},
        content="Paris has 9 million residents",
    )

    assert registration.source is None
    assert registry.tool_results_offered == 1
    assert registry.tool_results_admitted == 0


def test_an_admitted_call_raises_both_counts() -> None:
    registry = SourceRegistry(turn_id=TURN)

    registration = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents.",
    )

    assert registration.source is not None
    assert registry.tool_results_offered == 1
    assert registry.tool_results_admitted == 1


def test_a_retried_identical_call_is_offered_again_but_not_admitted_again() -> None:
    """The bug Codex's plan review caught: ``_register`` reuses an existing source for a
    D4 retry's resubmitted content, so a naive per-call counter would overcount.
    """
    registry = SourceRegistry(turn_id=TURN)

    first = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents.",
    )
    second = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents.",
    )

    assert first.source is not None
    assert second.source is not None
    assert first.source.identifier == second.source.identifier, "the retry reuses the source"
    assert registry.tool_results_offered == 2
    assert registry.tool_results_admitted == 1


def test_admitted_reconciles_against_the_tool_and_documentation_source_count() -> None:
    """AC-3's claim, proven structurally rather than trusted: admitted always equals the
    count of registered sources whose kind is TOOL or DOCUMENTATION.
    """
    registry = SourceRegistry(turn_id=TURN)

    registry.register_tool_result(
        tool_name="bash",
        arguments={"command": "echo 'no'"},
        content="no",
    )
    registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/a"},
        content="A page about Paris.",
    )
    registry.register_tool_result(
        tool_name="get_library_docs",
        arguments={"library": "httpx", "topic": "timeouts"},
        content="AsyncClient accepts a timeout argument.",
    )
    registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/a"},
        content="A page about Paris.",
    )
    registry.register_memory_item({"name": "Paris", "description": "Capital of France."})

    tool_kind_sources = [
        s for s in registry.sources() if s.kind in (SourceKind.TOOL, SourceKind.DOCUMENTATION)
    ]
    assert registry.tool_results_admitted == len(tool_kind_sources) == 2
    assert registry.tool_results_offered == 4
