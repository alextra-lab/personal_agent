"""FRE-1346 (ADR-0098 Amendment A · A4/A4b) — source derivation and containment association.

The association seam decided in Python at write time: which retrieved source justifies
which extracted item. These are the unit-level proofs for AC-1's three seeded negatives
(universal-``none``, turn-level attribution, and URL-echo attribution), plus the
provenance-version vs corroborating-authority identity split A4b requires.
"""

# ruff: noqa: D103

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from personal_agent.memory.provenance import (
    SourceRecord,
    associate,
    attribution_for_relationship,
    sources_from_tool_results,
)
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.types import ToolDefinition

_TS = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

_PAGE = (
    "SafeCart is a checkout platform used by retailers. "
    "The SafeCart engineering team is based in Lisbon."
)


def _registry() -> ToolRegistry:
    """A registry with one referent-declaring tool and one that declares none."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="fetch_url",
            description="fetch",
            category="network",
            parameters=[],
            risk_level="medium",
            allowed_modes=["NORMAL"],
            referent_parameter="url",
        ),
        lambda **_: None,
    )
    registry.register(
        ToolDefinition(
            name="web_search",
            description="search",
            category="network",
            parameters=[],
            risk_level="low",
            allowed_modes=["NORMAL"],
        ),
        lambda **_: None,
    )
    return registry


def _tool_results(
    *,
    url: str = "https://example.com/platforms",
    text: str = _PAGE,
    success: bool = True,
) -> list[dict[str, object]]:
    return [
        {
            "tool_name": "fetch_url",
            "success": success,
            "output": {"url": url, "text": text, "char_count": len(text), "truncated": False},
            "error": None,
            "latency_ms": 12.0,
            "arguments": {"url": url},
        }
    ]


def _sources(**kwargs: object) -> list[SourceRecord]:
    return sources_from_tool_results(
        _tool_results(**kwargs),  # type: ignore[arg-type]
        retrieved_at=_TS,
        capture_trace_id="trace-1",
        tool_registry=_registry(),
    )


# --------------------------------------------------------------------------------------
# Derivation — which tool results become :Source records at all
# --------------------------------------------------------------------------------------


def test_a_referent_declaring_tool_result_becomes_a_source() -> None:
    sources = _sources()
    assert len(sources) == 1
    assert sources[0].referent == "https://example.com/platforms"
    assert sources[0].retrieved_at == _TS
    assert sources[0].retained_pointer == "capture://trace-1#tool_results/0"


def test_a_tool_declaring_no_referent_yields_no_source() -> None:
    """A2: web_search addresses a query, not a referent — it has no external address."""
    results = [
        {
            "tool_name": "web_search",
            "success": True,
            "output": {"results": [{"title": "SafeCart"}]},
            "error": None,
            "latency_ms": 5.0,
            "arguments": {"query": "SafeCart"},
        }
    ]
    assert (
        sources_from_tool_results(
            results,
            retrieved_at=_TS,
            capture_trace_id="trace-1",
            tool_registry=_registry(),
        )
        == []
    )


def test_a_failed_tool_result_yields_no_source() -> None:
    """A failed fetch retrieved nothing; its error text is not retrieved content."""
    assert _sources(success=False) == []


def test_an_unregistered_tool_yields_no_source() -> None:
    results = [
        {
            "tool_name": "some_future_tool",
            "success": True,
            "output": {"text": "SafeCart"},
            "error": None,
            "latency_ms": 5.0,
            "arguments": {"url": "https://example.com"},
        }
    ]
    assert (
        sources_from_tool_results(
            results,
            retrieved_at=_TS,
            capture_trace_id="trace-1",
            tool_registry=_registry(),
        )
        == []
    )


# --------------------------------------------------------------------------------------
# A4b — the two identities, deliberately separated
# --------------------------------------------------------------------------------------


def test_same_referent_and_content_is_the_same_source_id() -> None:
    """Provenance-version identity is (referent, content_hash) — a refetch is one Source."""
    first = _sources()[0]
    second = _sources()[0]
    assert first.source_id == second.source_id


def test_changed_content_at_the_same_referent_mints_a_new_source_id() -> None:
    """'The page moved under the claim' becomes detectable rather than silent."""
    original = _sources()[0]
    changed = _sources(text=_PAGE + " It was acquired in 2026.")[0]
    assert changed.source_id != original.source_id
    assert changed.referent == original.referent


def test_two_versions_of_one_page_are_one_authority() -> None:
    """Corroborating-authority identity is the referent's host, NOT the version.

    A4b states this separately because collapsing the two would let a single page
    changing over time satisfy D6's second-distinct-source requirement — repetition
    wearing a new hash.
    """
    original = _sources()[0]
    changed = _sources(text=_PAGE + " It was acquired in 2026.")[0]
    assert original.authority == changed.authority == "example.com"


def test_authority_falls_back_to_the_referent_when_it_is_not_a_url() -> None:
    record = SourceRecord.build(
        referent="ingested-document-42",
        content="SafeCart is a checkout platform.",
        retrieved_at=_TS,
        retained_pointer="capture://trace-1#tool_results/0",
    )
    assert record.authority == "ingested-document-42"


@pytest.mark.parametrize("referent", ["http://[abc", "http://[::1", "https://["])
def test_a_malformed_referent_does_not_abort_the_consolidation_pass(referent: str) -> None:
    """``urlsplit`` raises on a malformed IPv6 literal, and the URL is model-chosen.

    Propagating that would lose every other item's provenance in the same sweep over one
    unparseable address.
    """
    record = SourceRecord.build(
        referent=referent,
        content="SafeCart is a checkout platform.",
        retrieved_at=_TS,
        retained_pointer="capture://trace-1#tool_results/0",
    )
    assert record.authority == referent


def test_content_never_reaches_the_cypher_map() -> None:
    """D3: Core holds the small keyed pointer; the bytes stay in the Docs layer."""
    payload = _sources()[0].to_cypher_map()
    assert "content" not in payload
    assert set(payload) == {
        "source_id",
        "referent",
        "authority",
        "retrieved_at",
        "content_hash",
        "content_hash_scope",
        "retained_pointer",
    }
    assert all(isinstance(value, str) for value in payload.values())


# --------------------------------------------------------------------------------------
# A4 — association by containment. AC-1's seeded negatives.
# --------------------------------------------------------------------------------------


def test_an_entity_named_in_the_page_is_associated_to_it() -> None:
    """AC-1 positive: the reference exists and it actually supports the item."""
    sources = _sources()
    assert associate("SafeCart", sources) == [sources[0].source_id]


def test_seeded_negative_a_a_contained_entity_must_not_fall_to_none() -> None:
    """AC-1(a): the universal-``none`` implementation must fail here.

    This is also the exact test that catches comparing ``check_containment``'s
    ``ContainmentResult`` to a ``ContainmentOutcome`` — written that way every
    comparison is false and every item silently falls to ``none``.
    """
    assert associate("SafeCart", _sources()) != []


def test_seeded_negative_b_an_absent_entity_gets_no_source() -> None:
    """AC-1(b): the turn-level shortcut — linking every entity to any URL the turn
    happened to fetch — must fail here.
    """
    assert associate("Kubernetes", _sources()) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/companies/AcmeWidgets",
        "https://example.com/wiki/Acme_Widgets_Inc",
        "https://example.com/search?q=AcmeWidgets",
    ],
)
def test_seeded_negative_c_an_entity_present_only_in_the_url_gets_no_source(url: str) -> None:
    """The address is not retrieved content.

    ``fetch_url`` echoes its own ``url`` argument in the result, so attributing against
    the raw result would make an entity named in the URL *contained* in a page that never
    mentions it — A4's false-association shape arriving through the address.

    The cases are **path and query segments**, deliberately: ``normalize_tokens`` keeps a
    dotted host as one token, so ``acmewidgets.example.com`` never matches ``AcmeWidgets``
    and a host-only fixture would pass with the echo strip removed. These three fire; a
    host-only one does not. (Verified by mutation — see the PR body.)
    """
    sources = _sources(url=url, text=_PAGE)
    assert associate("AcmeWidgets", sources) == []
    assert associate("Acme Widgets Inc", sources) == []


def test_only_contained_creates_a_reference() -> None:
    """ENTAILMENT_REQUIRED and UNVERIFIABLE create no reference (A4).

    An entity-free predicate is CONTAINED-but-escalated; an attribution that needed an
    entailment judgement is not the mechanical, model-independent link this rule provides.
    """
    assert associate("is widely used", _sources()) == []


def test_an_item_matching_two_sources_carries_both() -> None:
    """Provenance is append-only; multiple matches are recorded, not treated as ambiguity."""
    results = _tool_results(url="https://a.example.com/x") + [
        {
            "tool_name": "fetch_url",
            "success": True,
            "output": {"url": "https://b.example.com/y", "text": _PAGE, "truncated": False},
            "error": None,
            "latency_ms": 9.0,
            "arguments": {"url": "https://b.example.com/y"},
        }
    ]
    sources = sources_from_tool_results(
        results,
        retrieved_at=_TS,
        capture_trace_id="trace-1",
        tool_registry=_registry(),
    )
    assert len(associate("SafeCart", sources)) == 2


def test_lowercase_stylized_names_fall_to_none_as_a_known_class() -> None:
    """A4 records this false-negative class rather than widening the check."""
    sources = _sources(text="We installed npm and shipped it.")
    assert associate("npm", sources) == []


# --------------------------------------------------------------------------------------
# Relationships — AC-1 applied to the relationship attribution string
# --------------------------------------------------------------------------------------


def test_relationship_attribution_verbalizes_the_predicate() -> None:
    assert (
        attribution_for_relationship("SafeCart", "BASED_IN", "Lisbon") == "SafeCart based in Lisbon"
    )


def test_a_contained_relationship_is_associated() -> None:
    sources = _sources()
    attribution = attribution_for_relationship("SafeCart", "BASED_IN", "Lisbon")
    assert associate(attribution, sources) == [sources[0].source_id]


def test_seeded_negative_a_relationship_contained_must_not_fall_to_none() -> None:
    """AC-1(a) for relationships — a broken relationship path must not pass silently."""
    assert (
        associate(attribution_for_relationship("SafeCart", "BASED_IN", "Lisbon"), _sources()) != []
    )


def test_seeded_negative_b_an_absent_relationship_gets_no_source() -> None:
    """AC-1(b) for relationships."""
    assert (
        associate(attribution_for_relationship("SafeCart", "BASED_IN", "Berlin"), _sources()) == []
    )


@pytest.mark.parametrize("attribution", ["", "   "])
def test_an_empty_attribution_string_never_associates(attribution: str) -> None:
    """An empty required set holding vacuously is the bug D3's predicate rule kills."""
    assert associate(attribution, _sources()) == []
