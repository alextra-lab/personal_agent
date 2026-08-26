"""The inline checks (ADR-0138 D3 / D2) — FRE-1282 AC-1, AC-2, AC-6.

Each gate is shown to reject **on its own**, with the outcome naming which one fired; and
a genuinely-grounded turn is shown to pass, which is what stops a reject-everything
implementation from acing the negatives.
"""

from __future__ import annotations

from personal_agent.grounding.citations import parse_citations
from personal_agent.grounding.source_registry import Entitlement, SourceRegistry
from personal_agent.grounding.spans import (
    NonExemptReason,
    Span,
    SpanExtraction,
    SpanLabel,
)
from personal_agent.grounding.verification import (
    CheckOutcome,
    Reachability,
    check_reachability,
    unavailable,
    verify_turn,
)

TURN = "trace-verify-0001"


def _non_exempt(output: str, text: str) -> SpanExtraction:
    """One non-exempt span covering ``text`` inside ``output``.

    Stands in for the extractor so these tests exercise verification rather than
    classification — the two are separately measured components on purpose.
    """
    start = output.index(text)
    return SpanExtraction(
        output=output,
        spans=(
            Span(
                start=start,
                end=start + len(text),
                text=text,
                label=SpanLabel.CLAIM_NON_EXEMPT,
                reason=NonExemptReason.CLASSIFIED,
            ),
        ),
    )


def _fetched(registry: SourceRegistry, url: str, content: str) -> str:
    """Register a fetched page and return its identifier."""
    registration = registry.register_tool_result(
        tool_name="fetch_url", arguments={"url": url}, content=content
    )
    assert registration.source is not None
    return registration.source.identifier


# ── AC-2 — the positive control, first ──────────────────────────────────────────────


def test_valid_citation_passes_and_delivers() -> None:
    """A real source that really contains the claim is delivered.

    Placed before the negatives deliberately: AC-1 is trivially satisfied by refusing
    everything, and this is the criterion that makes refusing everything fail.
    """
    registry = SourceRegistry(turn_id=TURN)
    ident = _fetched(
        registry,
        "https://example.com/paris",
        "Paris counts 2,100,000 residents within the city limits.",
    )
    output = f"Paris has 2.1 million residents [{ident}]."

    result = verify_turn(
        _non_exempt(output, "Paris has 2.1 million residents"), parse_citations(output), registry
    )

    assert [span.outcome for span in result.spans] == [CheckOutcome.PASSED]
    assert result.compliant is True
    assert result.failures == ()


# ── AC-1 — each gate rejects independently, naming itself ───────────────────────────


def test_unresolvable_identifier_rejects() -> None:
    """D3(a) — a well-formed identifier this turn never minted resolves to nothing."""
    registry = SourceRegistry(turn_id=TURN)
    _fetched(registry, "https://example.com/paris", "Paris counts 2,100,000 residents.")
    stale = "S9@" + "0" * 16
    output = f"Paris has 2.1 million residents [{stale}]."

    result = verify_turn(
        _non_exempt(output, "Paris has 2.1 million residents"), parse_citations(output), registry
    )

    assert result.spans[0].outcome is CheckOutcome.UNRESOLVED
    assert "D3(a)" in result.spans[0].detail


def test_unreachable_source_rejects() -> None:
    """D3(b) — a 200-response that is really a soft-404 is not a reachable source."""
    registry = SourceRegistry(turn_id=TURN)
    ident = _fetched(
        registry, "https://example.com/gone", "Page not found. The article is no longer available."
    )
    output = f"Paris has 2.1 million residents [{ident}]."

    result = verify_turn(
        _non_exempt(output, "Paris has 2.1 million residents"), parse_citations(output), registry
    )

    assert result.spans[0].outcome is CheckOutcome.UNREACHABLE
    assert result.spans[0].reachability is Reachability.UNREACHABLE
    assert "D3(b)" in result.spans[0].detail


def test_uncontained_source_rejects() -> None:
    """D3(c) — real, reachable, and about something else entirely."""
    registry = SourceRegistry(turn_id=TURN)
    ident = _fetched(registry, "https://example.com/lyon", "Lyon is France's third largest city.")
    output = f"Paris has 2.1 million residents [{ident}]."

    result = verify_turn(
        _non_exempt(output, "Paris has 2.1 million residents"), parse_citations(output), registry
    )

    assert result.spans[0].outcome is CheckOutcome.NOT_CONTAINED
    assert "D3(c)" in result.spans[0].detail


def test_the_three_rejections_are_distinguishable() -> None:
    """AC-1's real requirement: not three failures, three *different* failures."""
    assert len({CheckOutcome.UNRESOLVED, CheckOutcome.UNREACHABLE, CheckOutcome.NOT_CONTAINED}) == 3


def test_uncited_assertion_rejects() -> None:
    """D1 default-deny — a non-exempt span carrying no marker at all."""
    registry = SourceRegistry(turn_id=TURN)
    output = "Paris has 2.1 million residents."

    result = verify_turn(
        _non_exempt(output, "Paris has 2.1 million residents"), parse_citations(output), registry
    )

    assert result.spans[0].outcome is CheckOutcome.UNCITED
    assert result.spans[0].identifier is None


# ── D2 — the source must be entitled to make the claim ──────────────────────────────


def test_agent_derived_memory_cannot_ground_a_claim() -> None:
    """The live 2026-08-26 finding: three green checks on the system's own confabulation.

    An ``Event`` node holding a date the agent hallucinated in an earlier session resolves,
    passes reachability vacuously, and *contains* the claim — because the source is the
    claim. Only entitlement stops it.
    """
    registry = SourceRegistry(turn_id=TURN)
    source = registry.register_memory_item(
        {
            "name": "Wednesday, July 1, 2026",
            "description": "Today's date is Wednesday, July 1, 2026.",
        }
    )
    output = f"Today is Wednesday, July 1, 2026 [{source.identifier}]."

    result = verify_turn(
        _non_exempt(output, "Today is Wednesday, July 1, 2026"), parse_citations(output), registry
    )

    assert source.entitlement is Entitlement.AGENT_DERIVED
    assert result.spans[0].outcome is CheckOutcome.SOURCE_NOT_ENTITLED
    assert "D2" in result.spans[0].detail


def test_user_stated_memory_still_grounds_a_claim() -> None:
    """The paired positive: denying agent-derived memory must not deny all memory.

    Without this, the entitlement gate would be indistinguishable from switching memory
    citation off.
    """
    registry = SourceRegistry(turn_id=TURN)
    source = registry.register_memory_item(
        {
            "name": "Ortiz preference",
            "description": "I buy Ortiz bonito for the pantry.",
            "asserted_by": "user",
        }
    )
    output = f"You buy Ortiz bonito for the pantry [{source.identifier}]."

    result = verify_turn(
        _non_exempt(output, "You buy Ortiz bonito for the pantry"),
        parse_citations(output),
        registry,
    )

    assert source.entitlement is Entitlement.USER_STATED
    assert result.spans[0].outcome is CheckOutcome.PASSED


def test_the_users_own_words_are_entitled() -> None:
    """D2 item 4 — and the exemption D4's terminal statement ultimately rests on."""
    registry = SourceRegistry(turn_id=TURN)
    source = registry.register_user_message("Book me a flight to Bilbao on Tuesday.")

    assert source is not None
    assert source.entitlement is Entitlement.USER_STATED


# ── D3(b) — vacuous where D2 says it is ─────────────────────────────────────────────


def test_sources_with_no_external_referent_pass_vacuously() -> None:
    """D2: for turn-local evidence "the recorded result *is* the durable artifact"."""
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="web_search",
        arguments={"query": "paris population"},
        content="Paris counts 2,100,000 residents.",
    )

    assert registration.source is not None
    assert registration.source.referent is None
    assert check_reachability(registration.source) is Reachability.NOT_APPLICABLE


def test_a_long_page_mentioning_not_found_is_still_reachable() -> None:
    """The length bound is what stops D3(b) manufacturing refusals."""
    registry = SourceRegistry(turn_id=TURN)
    body = (
        "Paris counts 2,100,000 residents. " * 40
        + "Readers who follow a stale link will see page not found."
    )
    registration = registry.register_tool_result(
        tool_name="fetch_url", arguments={"url": "https://example.com/paris"}, content=body
    )

    assert registration.source is not None
    assert check_reachability(registration.source) is Reachability.REACHABLE


# ── AC-6 — the two failure families stay apart ──────────────────────────────────────


def test_unverifiable_and_true_no_source_are_distinct_in_the_record() -> None:
    """AC-6 — a normalizer limit must never be countable as honest not-knowing.

    One turn carrying both kinds: a paraphrased predicate against a supporting source, and
    a claim with no citation at all.
    """
    registry = SourceRegistry(turn_id=TURN)
    ident = _fetched(
        registry,
        "https://example.com/paris",
        "Paris counts 2,100,000 residents within the city limits.",
    )
    paraphrase = "Paris has 2.1 million inhabitants"
    uncited = "Lyon has 500,000 residents"
    output = f"{paraphrase} [{ident}]. {uncited}."

    extraction = SpanExtraction(
        output=output,
        spans=(
            Span(
                start=output.index(paraphrase),
                end=output.index(paraphrase) + len(paraphrase),
                text=paraphrase,
                label=SpanLabel.CLAIM_NON_EXEMPT,
                reason=NonExemptReason.CLASSIFIED,
            ),
            Span(
                start=output.index(uncited),
                end=output.index(uncited) + len(uncited),
                text=uncited,
                label=SpanLabel.CLAIM_NON_EXEMPT,
                reason=NonExemptReason.CLASSIFIED,
            ),
        ),
    )

    result = verify_turn(extraction, parse_citations(output), registry)

    assert [span.text for span in result.unverifiable] == [paraphrase]
    assert [span.text for span in result.true_no_source] == [uncited]
    assert set(result.unverifiable).isdisjoint(result.true_no_source)


def test_the_system_citing_itself_counts_as_having_no_source() -> None:
    """``SOURCE_NOT_ENTITLED`` is a no-source outcome, not a containment limit.

    However well the tokens matched, a claim resting on the system's own earlier utterance
    has no admissible provenance — so it must not land in the bucket reserved for our
    normalizer's shortcomings.
    """
    registry = SourceRegistry(turn_id=TURN)
    source = registry.register_memory_item(
        {"name": "July 1", "description": "Today's date is Wednesday, July 1, 2026."}
    )
    output = f"Today is Wednesday, July 1, 2026 [{source.identifier}]."

    result = verify_turn(
        _non_exempt(output, "Today is Wednesday, July 1, 2026"), parse_citations(output), registry
    )

    assert len(result.true_no_source) == 1
    assert result.unverifiable == ()


def test_verification_that_could_not_run_is_not_compliant() -> None:
    """An unmeasured turn must never count as a passing one (FRE-1284's numerator)."""
    result = unavailable("span_extraction budget reservation denied")

    assert result.available is False
    assert result.compliant is False
    assert result.unavailable_reason == "span_extraction budget reservation denied"


def test_exempt_spans_are_never_given_a_verdict() -> None:
    """D1 excuses them; inventing a verdict would move coverage out of the extractor."""
    registry = SourceRegistry(turn_id=TURN)
    output = "Here is a plan you can run."
    extraction = SpanExtraction(
        output=output,
        spans=(Span(start=0, end=len(output), text=output, label=SpanLabel.NOT_A_CLAIM),),
    )

    result = verify_turn(extraction, parse_citations(output), registry)

    assert result.spans == ()
    assert result.compliant is True
