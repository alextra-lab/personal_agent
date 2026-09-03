"""FRE-1302: end-to-end proof that search_memory's Claims carry real entitlement.

Mirrors test_stance_entitlement_e2e.py's shape (FRE-1299, the push-path sibling): a synthetic
query_claims-shaped row (the Cypher round-trip is pinned separately in test_query_claims.py's
mocked-driver tests) is carried through the exact top-level JSON shape
tools/memory_search.py's search_memory_executor emits, into the real registry and verifier —
proving the whole pull path denies an agent-derived Claim rather than registering it as the
most-trusted EXTERNAL tier (the bug this ticket closes).

FRE-1347 extends the same proof to search_memory's ``entities`` (ADR-0098 Amendment A6):
entitlement follows the terminus of the provenance chain, not merely Claim authorship.
"""

from __future__ import annotations

import json

from personal_agent.grounding.citations import parse_citations
from personal_agent.grounding.source_registry import (
    USER_STATED_EXTRACTOR_SENTINEL,
    Entitlement,
    SourceRegistry,
)
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


def _search_memory_output(
    claims: list[dict[str, object]], entities: list[dict[str, object]] | None = None
) -> str:
    """The exact top-level shape search_memory_executor emits (tools/memory_search.py)."""
    output: dict[str, object] = {
        "matched_turns": [],
        "entities_found": len(entities or []),
        "total_turns": 0,
        "query_path": "entity_match",
        "claims": claims,
    }
    if entities is not None:
        output["entities"] = entities
    return json.dumps(output)


def _entity_row(
    name: str, *, provenance_state: str, extractor_model: str | None = "qwen3-8b"
) -> dict[str, object]:
    """Stand in for the entity dict tools/memory_search.py now emits (FRE-1347).

    ``extractor_model`` distinguishes an agent-extracted entity (a model identifier,
    the default here) from one written via the gateway's ``store_fact`` path
    (:data:`USER_STATED_EXTRACTOR_SENTINEL` -- user-provided, ADR-0098 Amendment A6's
    "a statement the owner made" terminus row for entities, which carry no
    ``asserted_by``). ``None`` here is deliberately its own, distinct case: it is what
    a legacy or bare-``MERGE``-fallback-created entity looks like on the wire (Neo4j has
    no persisted null, so "never set" and "explicitly None" are indistinguishable) --
    it must deny, not be read as the sentinel.
    """
    return {
        "name": name,
        "type": "Organization",
        "description": None,
        "mentions": 1,
        "provenance_state": provenance_state,
        "source_referents": ["https://example.com/vendor"]
        if provenance_state == "provenanced"
        else [],
        "extractor_model": extractor_model,
    }


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


# ---------------------------------------------------------------------------
# FRE-1347 — entities: entitlement follows the terminus (ADR-0098 Amendment A6)
# ---------------------------------------------------------------------------


def _run_entity_chain(
    *, mention_text: str, entities: list[dict[str, object]]
) -> tuple[Entitlement, CheckOutcome]:
    """search_memory entities-shaped output -> register_tool_result -> verify_turn."""
    content = _search_memory_output([], entities=entities)

    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="search_memory",
        arguments={"query_text": mention_text[:20]},
        content=content,
    )
    assert registration.source is not None
    source = registration.source

    output = f"{mention_text} [{source.identifier}]."
    verification = verify_turn(_non_exempt(output, mention_text), parse_citations(output), registry)

    return source.entitlement, verification.spans[0].outcome


def test_entity_terminating_at_fetched_page_is_external_and_citable() -> None:
    """AC-1, scenario 1: a SOURCED_FROM-linked entity's chain terminates externally."""
    entitlement, outcome = _run_entity_chain(
        mention_text="SafeCart",
        entities=[_entity_row("SafeCart", provenance_state="provenanced")],
    )

    assert entitlement is Entitlement.EXTERNAL
    assert outcome is CheckOutcome.PASSED


def test_entity_written_via_store_fact_is_user_stated() -> None:
    """AC-1, scenario 2 (entity path): ADR-0098 A6's owner-statement terminus row.

    Entities carry no ``asserted_by`` (that axis is Claim/Stance-only), so
    ``create_entity`` stamps :data:`USER_STATED_EXTRACTOR_SENTINEL` when its own
    ``extractor_model`` argument is ``None`` (the gateway's ``store_fact`` path --
    user-provided, no extraction; ``memory/service.py:2084-2117``) -- reused here
    rather than inventing a new field.
    """
    entitlement, outcome = _run_entity_chain(
        mention_text="EaseCert",
        entities=[
            _entity_row(
                "EaseCert",
                provenance_state="none",
                extractor_model=USER_STATED_EXTRACTOR_SENTINEL,
            )
        ],
    )

    assert entitlement is Entitlement.USER_STATED
    assert outcome is CheckOutcome.PASSED


def test_legacy_entity_with_no_extractor_model_property_is_refused() -> None:
    """The seeded negative for the sentinel design (feature-dev:code-reviewer finding).

    A pre-FRE-1347 legacy entity, or one written by the bare-``MERGE`` fallback in
    ``create_conversation`` (which predates ``extractor_model`` entirely), reads back
    with the property genuinely absent -- ``None`` on the wire, identical to what
    :data:`USER_STATED_EXTRACTOR_SENTINEL`'s absence-vs-presence collapse would produce
    if the classifier matched on absence rather than the sentinel's exact value. Must
    deny, not be mistaken for a store_fact write -- this is the over-admission the
    review that found this design gap was guarding against.
    """
    entitlement, outcome = _run_entity_chain(
        mention_text="Consolidated Widgets",
        entities=[
            _entity_row("Consolidated Widgets", provenance_state="none", extractor_model=None)
        ],
    )

    assert entitlement is Entitlement.AGENT_DERIVED
    assert outcome is CheckOutcome.SOURCE_NOT_ENTITLED


def test_entity_terminating_at_agent_authored_turn_is_refused() -> None:
    """AC-1 scenario 3 + AC-2: an entity with no external referent is not citable.

    This is the FRE-1338 regression proof: before this fix, an entity-only recall
    (no Claims) fell to the "no claims -> EXTERNAL" branch unconditionally, so a
    bare, agent-extracted, unprovenanced entity name would have passed verification
    at the most-trusted tier.
    """
    entitlement, outcome = _run_entity_chain(
        mention_text="Consolidated Widgets",
        entities=[
            _entity_row("Consolidated Widgets", provenance_state="none", extractor_model="qwen3-8b")
        ],
    )

    assert entitlement is Entitlement.AGENT_DERIVED
    assert outcome is CheckOutcome.SOURCE_NOT_ENTITLED


def test_mixed_recall_inherits_least_entitled_entity() -> None:
    """AC-1 scenario 4: one provenanced entity + one none-terminus entity in the same call.

    Both entities register under the same call-level entitlement (FRE-1280: one source
    per call), so the whole call must inherit the *worse* of the two -- never the better.
    """
    entitlement, outcome = _run_entity_chain(
        mention_text="SafeCart",
        entities=[
            _entity_row("SafeCart", provenance_state="provenanced"),
            _entity_row("Consolidated Widgets", provenance_state="none"),
        ],
    )

    assert entitlement is Entitlement.AGENT_DERIVED
    assert outcome is CheckOutcome.SOURCE_NOT_ENTITLED


def test_malformed_entities_shape_denies() -> None:
    """A non-list ``entities`` value denies rather than falling through to EXTERNAL."""
    content = json.dumps(
        {
            "matched_turns": [],
            "entities_found": 0,
            "total_turns": 0,
            "query_path": "entity_match",
            "claims": [],
            "entities": {"not": "a list"},
        }
    )

    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="search_memory",
        arguments={"query_text": "whatever"},
        content=content,
    )

    assert registration.source is not None
    assert registration.source.entitlement is Entitlement.AGENT_DERIVED
