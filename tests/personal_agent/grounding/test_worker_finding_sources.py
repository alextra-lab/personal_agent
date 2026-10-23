"""A sub-agent finding registers a source only from a retrieval that returned it (FRE-442).

ADR-0150 D1 gives each worker finding exactly one ``source_url``. ADR-0138 D2 decides what
that URL may register: only what the worker's own tool results returned. A URL the worker
wrote from its own knowledge is the model's words wearing a source's identifier.

Each positive control here is paired with its seeded negative, per ADR-0138's testing
strategy.
"""

from __future__ import annotations

import json

from personal_agent.grounding.source_registry import (
    Admissibility,
    Entitlement,
    RetrievedToolResult,
    SourceKind,
    SourceRegistry,
)

TURN = "trace-worker-findings"
KAYAK = "https://www.kayak.com/flights/PAR-NYC"
EXPEDIA = "https://www.expedia.com/deals/paris"
BUSINESS = "https://businessclasstravel.us/paris-new-york"


def _search_result() -> RetrievedToolResult:
    """One ``web_search`` result, in the contract shape ``render_content`` emits."""
    return RetrievedToolResult(
        tool_name="web_search",
        arguments={"query": "Paris New York business class price"},
        content=json.dumps(
            {
                "ok": True,
                "results": [
                    {
                        "title": "Paris to New York flights",
                        "url": KAYAK,
                        "snippet": "Business class from EUR 2,410 round trip.",
                        "engine": "braveapi",
                        "score": 1.0,
                    },
                    {
                        "title": "Paris deals",
                        "url": EXPEDIA,
                        "snippet": "Round trips from EUR 1,980 in business.",
                        "engine": "exa",
                        "score": 0.8,
                    },
                ],
                "result_count": 2,
                "query": "Paris New York business class price",
            }
        ),
    )


def _fetch_result() -> RetrievedToolResult:
    """One ``fetch_url`` result: the referent is the ``url`` argument."""
    return RetrievedToolResult(
        tool_name="fetch_url",
        arguments={"url": BUSINESS},
        content=json.dumps(
            {
                "ok": True,
                "url": BUSINESS,
                "text": "Air France business class Paris to New York: EUR 3,105.",
                "char_count": 54,
                "truncated": False,
            }
        ),
    )


# ── AC-1: findings from the worker's own results register ──────────────────────────────


def test_two_findings_from_one_search_register_two_sources_with_their_urls() -> None:
    """Two result entries of one search are two sources, each narrowed to its own entry."""
    registry = SourceRegistry(turn_id=TURN)
    retrieved = (_search_result(),)

    kayak = registry.register_worker_finding(source_url=KAYAK, retrieved=retrieved)
    expedia = registry.register_worker_finding(source_url=EXPEDIA, retrieved=retrieved)

    assert kayak.admissibility is Admissibility.ADMISSIBLE
    assert expedia.admissibility is Admissibility.ADMISSIBLE
    assert kayak.source is not None and expedia.source is not None
    assert kayak.source.identifier != expedia.source.identifier
    assert kayak.source.referent == KAYAK
    assert expedia.source.referent == EXPEDIA
    assert "EUR 2,410" in kayak.source.content
    assert "EUR 1,980" not in kayak.source.content
    assert kayak.source.kind is SourceKind.TOOL
    assert kayak.source.entitlement is Entitlement.EXTERNAL
    assert kayak.source.label == "sub_agent:web_search"
    assert len(registry.sources()) == 2


def test_a_fetched_page_registers_through_its_referent_argument() -> None:
    """``fetch_url`` declares ``referent_parameter="url"``: the page is the source."""
    registry = SourceRegistry(turn_id=TURN)

    registration = registry.register_worker_finding(
        source_url=BUSINESS, retrieved=(_search_result(), _fetch_result())
    )

    assert registration.source is not None
    assert registration.source.referent == BUSINESS
    assert "EUR 3,105" in registration.source.content


def test_only_the_site_root_tolerates_a_trailing_slash() -> None:
    """``/path`` and ``/path/`` can be two resources, so only the root is folded."""
    root = RetrievedToolResult(
        tool_name="web_search",
        arguments={"query": "kayak"},
        content=json.dumps({"results": [{"url": "https://www.kayak.com"}]}),
    )
    registry = SourceRegistry(turn_id=TURN)

    assert (
        registry.register_worker_finding(source_url="https://www.kayak.com/", retrieved=(root,))
    ).source is not None
    assert (
        registry.register_worker_finding(source_url=f"{KAYAK}/", retrieved=(_search_result(),))
    ).source is None


def test_two_pages_with_identical_bodies_keep_their_own_urls() -> None:
    """Dedupe must not hand one page's URL to a citation of another (codex plan review)."""
    body = "Fares from EUR 2,410."
    retrieved = tuple(
        RetrievedToolResult(
            tool_name="fetch_url",
            arguments={"url": url},
            content=json.dumps({"ok": True, "url": url, "text": body}),
        )
        for url in (KAYAK, EXPEDIA)
    )
    registry = SourceRegistry(turn_id=TURN)

    kayak = registry.register_worker_finding(source_url=KAYAK, retrieved=retrieved)
    expedia = registry.register_worker_finding(source_url=EXPEDIA, retrieved=retrieved)

    assert kayak.source is not None and expedia.source is not None
    assert kayak.source.referent == KAYAK
    assert expedia.source.referent == EXPEDIA


def test_two_findings_citing_one_url_share_one_source() -> None:
    """The same page cited twice is one source, not two ordinals for the same thing."""
    registry = SourceRegistry(turn_id=TURN)
    retrieved = (_search_result(),)

    first = registry.register_worker_finding(source_url=KAYAK, retrieved=retrieved)
    second = registry.register_worker_finding(source_url=KAYAK, retrieved=retrieved)

    assert first.source is not None and second.source is not None
    assert first.source.identifier == second.source.identifier
    assert len(registry.sources()) == 1


def test_each_finding_is_one_offer_so_admitted_never_exceeds_offered() -> None:
    """ADR-0139 D1's reconciliation holds with worker findings in the registry."""
    registry = SourceRegistry(turn_id=TURN)
    retrieved = (_search_result(),)

    registry.register_worker_finding(source_url=KAYAK, retrieved=retrieved)
    registry.register_worker_finding(source_url="https://invented.example/x", retrieved=retrieved)

    assert registry.tool_results_offered == 2
    assert registry.tool_results_admitted == 1


# ── AC-2: no invented source (the seeded negative) ─────────────────────────────────────


def test_a_url_the_worker_never_retrieved_registers_nothing() -> None:
    """The seeded negative: a plausible URL absent from every result is not a source."""
    registry = SourceRegistry(turn_id=TURN)

    registration = registry.register_worker_finding(
        source_url="https://www.airfrance.com/fares/paris-new-york",
        retrieved=(_search_result(), _fetch_result()),
    )

    assert registration.source is None
    assert registration.admissibility is Admissibility.NOT_RETRIEVED
    assert registry.sources() == ()
    assert "sub_agent_finding:not_retrieved" in registry.refused_origin_admissibility


def test_a_worker_with_no_retrievals_registers_nothing() -> None:
    """A worker that ran no tool has nothing a finding could cite."""
    registry = SourceRegistry(turn_id=TURN)

    registration = registry.register_worker_finding(source_url=KAYAK, retrieved=())

    assert registration.source is None
    assert registration.admissibility is Admissibility.NOT_RETRIEVED


def test_a_url_found_only_inside_page_text_is_not_a_retrieval() -> None:
    """A page that links to a URL did not retrieve it: the worker never read that page."""
    registry = SourceRegistry(turn_id=TURN)
    page = RetrievedToolResult(
        tool_name="fetch_url",
        arguments={"url": BUSINESS},
        content=json.dumps({"ok": True, "url": BUSINESS, "text": f"See also {KAYAK} for fares."}),
    )

    registration = registry.register_worker_finding(source_url=KAYAK, retrieved=(page,))

    assert registration.source is None
    assert registration.admissibility is Admissibility.NOT_RETRIEVED


def test_a_url_field_inside_a_fetched_json_page_is_not_a_retrieval() -> None:
    """A fetched page served as JSON can carry ``url`` fields: links, not fetches."""
    registry = SourceRegistry(turn_id=TURN)
    page = RetrievedToolResult(
        tool_name="fetch_url",
        arguments={"url": BUSINESS},
        content=json.dumps({"ok": True, "url": BUSINESS, "text": "", "links": [{"url": KAYAK}]}),
    )

    registration = registry.register_worker_finding(source_url=KAYAK, retrieved=(page,))

    assert registration.source is None
    assert registration.admissibility is Admissibility.NOT_RETRIEVED


def test_a_url_printed_by_arbitrary_code_is_refused() -> None:
    """D2: ``run_python`` output is the model's own words, whatever URL it prints."""
    registry = SourceRegistry(turn_id=TURN)
    printed = RetrievedToolResult(
        tool_name="run_python",
        arguments={"code": f"print({{'url': '{KAYAK}'}})"},
        content=json.dumps({"ok": True, "url": KAYAK, "stdout": "EUR 99"}),
    )

    registration = registry.register_worker_finding(source_url=KAYAK, retrieved=(printed,))

    assert registration.source is None
    assert registration.admissibility is Admissibility.MODEL_AUTHORED_INVOCATION
    assert registry.sources() == ()


def test_a_truncated_search_does_not_match_inside_its_partial_text() -> None:
    """A truncated result is a JSON prefix in a string, so no ``url`` object can be read."""
    registry = SourceRegistry(turn_id=TURN)
    truncated = RetrievedToolResult(
        tool_name="web_search",
        arguments={"query": "fares"},
        content=json.dumps(
            {"ok": True, "truncated": True, "partial": json.dumps({"results": [{"url": KAYAK}]})}
        ),
    )

    registration = registry.register_worker_finding(source_url=KAYAK, retrieved=(truncated,))

    assert registration.source is None
