"""FRE-1302: end-to-end proof that search_memory's Claims carry real entitlement.

Mirrors test_stance_entitlement_e2e.py's shape (FRE-1299, the push-path sibling): a synthetic
query_claims-shaped row (the Cypher round-trip is pinned separately in test_query_claims.py's
mocked-driver tests) is carried through the exact top-level JSON shape
tools/memory_search.py's search_memory_executor emits, into the real registry and verifier —
proving the whole pull path denies an agent-derived Claim rather than registering it as the
most-trusted EXTERNAL tier (the bug this ticket closes).
"""

from __future__ import annotations

import json

from personal_agent.grounding.citations import parse_citations
from personal_agent.grounding.source_registry import Entitlement, SourceRegistry
from personal_agent.grounding.spans import NonExemptReason, Span, SpanExtraction, SpanLabel
from personal_agent.grounding.verification import CheckOutcome, verify_turn

TURN = "trace-fre1302-e2e"


def _claim_row(claim_id: str, content: str, asserted_by: str | None) -> dict[str, object]:
    """Stand in for query_claims' Cypher round-trip (pinned separately in
    test_query_claims.py's mocked-driver tests) -- the same canonicalization that method
    applies when reading the property back off the node.
    """
    return {
        "claim_id": claim_id,
        "content": content,
        "confidence": 0.8,
        "knowledge_class": "Personal",
        "observed_at": "2026-08-26T00:00:00Z",
        "asserted_by": "user" if asserted_by == "user" else "agent",
    }


def _search_memory_output(claims: list[dict[str, object]]) -> str:
    """The exact top-level shape search_memory_executor emits (tools/memory_search.py)."""
    return json.dumps(
        {
            "matched_turns": [],
            "entities_found": 0,
            "total_turns": 0,
            "query_path": "entity_match",
            "claims": claims,
        }
    )


def _non_exempt(output: str, text: str) -> SpanExtraction:
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


def _run_chain(*, claim_content: str, asserted_by: str | None) -> tuple[Entitlement, CheckOutcome]:
    """query_claims-shaped row -> search_memory JSON -> register_tool_result -> verify_turn,
    in that order, with only Neo4j itself standing in for a hand-built row.
    """
    row = _claim_row("c1", claim_content, asserted_by)
    content = _search_memory_output([row])

    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="search_memory",
        arguments={"query_text": claim_content[:20]},
        content=content,
    )
    assert registration.source is not None
    source = registration.source

    output = f"{claim_content} [{source.identifier}]."
    verification = verify_turn(
        _non_exempt(output, claim_content), parse_citations(output), registry
    )

    return source.entitlement, verification.spans[0].outcome


def test_user_asserted_claim_via_search_memory_passes_verification() -> None:
    """The positive control: a user-stated Claim survives every layer and is citable.

    Phrased with an entity and a figure (rather than "the lease ends in june", which has
    neither) so containment resolves directly and D3(d) inline entailment never engages —
    that check is FRE-1286's own surface, not this test's.
    """
    entitlement, outcome = _run_chain(
        claim_content="Ortiz bonito costs 12 euros", asserted_by="user"
    )

    assert entitlement is Entitlement.USER_STATED
    assert outcome is CheckOutcome.PASSED


def test_agent_derived_claim_via_search_memory_is_refused() -> None:
    """AC-3, the regression proof this ticket exists for.

    Before this fix, register_tool_result hardcoded EXTERNAL for every search_memory call,
    so this agent-derived Claim would have passed verification at the *most* trusted tier —
    worse than an unlabelled Stance, which denies by default (FRE-1299).
    """
    entitlement, outcome = _run_chain(claim_content="the tenant seems unhappy", asserted_by="agent")

    assert entitlement is Entitlement.AGENT_DERIVED
    assert outcome is CheckOutcome.SOURCE_NOT_ENTITLED


def test_legacy_claim_with_no_authorship_property_is_refused() -> None:
    """A pre-FRE-1302 Claim (no asserted_by property at all) still denies."""
    entitlement, outcome = _run_chain(claim_content="the deposit is 1200 euros", asserted_by=None)

    assert entitlement is Entitlement.AGENT_DERIVED
    assert outcome is CheckOutcome.SOURCE_NOT_ENTITLED
