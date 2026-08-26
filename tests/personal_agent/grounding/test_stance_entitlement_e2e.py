"""FRE-1299: end-to-end proof that Stance co-authorship survives the whole recall pipeline.

Every layer covered separately in ``test_stance_authorship.py`` (extraction stamping),
``test_claims_stance_cypher.py`` (Cypher write/read), ``test_context.py`` (item-builder), and
``test_verification.py`` (entitlement/verification) starts from an already-correct synthetic
input at that layer — nothing proves the layers actually compose. This chains them: the real
producer functions, the real item builders, and a hand-built row dict standing in for the one
line of the Cypher round-trip that needs a live Neo4j (the mocked-driver tests already pin
that Cypher's shape).
"""

from __future__ import annotations

from datetime import datetime, timezone

from personal_agent.grounding.citations import parse_citations
from personal_agent.grounding.source_registry import Entitlement, SourceRegistry
from personal_agent.grounding.spans import NonExemptReason, Span, SpanExtraction, SpanLabel
from personal_agent.grounding.verification import CheckOutcome, verify_turn
from personal_agent.memory.models import Stance
from personal_agent.request_gateway.context import _stance_context_items
from personal_agent.second_brain.consolidator import _build_stance
from personal_agent.second_brain.entity_extraction import _finalize_extraction

TURN = "trace-fre1299-e2e"
_TURN_TS = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def _row_from_stance(stance: Stance) -> dict[str, object]:
    """Stand in for query_current_stances' Cypher round-trip (pinned separately in
    test_claims_stance_cypher.py's mocked-driver tests) -- the same canonicalization
    that method applies when reading the property back off the edge.
    """
    return {
        "target": stance.target,
        "affect": stance.affect,
        "mastery": stance.mastery,
        "asserted_by": "user" if stance.asserted_by == "user" else "agent",
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


def _run_chain(
    *, affect: str, user_message: str, assistant_response: str
) -> tuple[Entitlement, CheckOutcome]:
    """Extraction -> _build_stance -> (simulated) Cypher round-trip -> item builder ->
    register_memory_item -> verify_turn, in that order, with only Neo4j itself mocked out.
    """
    result: dict[str, object] = {
        "entities": [],
        "stances": [{"target": "Ortiz bonito", "affect": affect}],
        "claims": [],
    }
    _finalize_extraction(
        result,
        trace_id="trace-e2e",
        session_id="session-e2e",
        turn_timestamp=_TURN_TS,
        user_message=user_message,
        assistant_response=assistant_response,
    )
    stance = _build_stance(result["stances"][0])  # type: ignore[index]
    assert stance is not None

    row = _row_from_stance(stance)
    items = _stance_context_items(["Ortiz bonito"], [row])
    assert len(items) == 1

    registry = SourceRegistry(turn_id=TURN)
    source = registry.register_memory_item(items[0])

    output = f"{affect} [{source.identifier}]."
    verification = verify_turn(_non_exempt(output, affect), parse_citations(output), registry)

    return source.entitlement, verification.spans[0].outcome


def test_owner_grounded_stance_chain_passes_verification() -> None:
    """AC-1, end to end: an owner-stated stance survives every layer and is citable."""
    entitlement, outcome = _run_chain(
        affect="loves Ortiz bonito",
        user_message="I really love Ortiz bonito for the pantry, always have.",
        assistant_response="Noted. The staging deploy needs a manual approval gate.",
    )

    assert entitlement is Entitlement.USER_STATED
    assert outcome is CheckOutcome.PASSED


def test_assistant_grounded_stance_chain_is_refused() -> None:
    """AC-2 (regression shape) — agent-derived content stays denied through the same chain."""
    entitlement, outcome = _run_chain(
        affect="loves Ortiz bonito",
        user_message="What should I put on the shopping list?",
        assistant_response="You should get Ortiz bonito -- you love it, based on prior visits.",
    )

    assert entitlement is Entitlement.AGENT_DERIVED
    assert outcome is CheckOutcome.SOURCE_NOT_ENTITLED


def test_legacy_stance_with_no_authorship_property_chain_is_refused() -> None:
    """AC-3, end to end: a pre-FRE-1299 edge (no asserted_by property at all) still denies."""
    legacy_stance = Stance(target="Ortiz bonito", affect="loves Ortiz bonito", observed_at=_TURN_TS)
    row = {
        "target": legacy_stance.target,
        "affect": legacy_stance.affect,
        "mastery": legacy_stance.mastery,
        # No "asserted_by" key at all -- the pre-FRE-1299 row shape.
    }
    items = _stance_context_items(["Ortiz bonito"], [row])
    registry = SourceRegistry(turn_id=TURN)
    source = registry.register_memory_item(items[0])
    output = f"loves Ortiz bonito [{source.identifier}]."

    verification = verify_turn(
        _non_exempt(output, "loves Ortiz bonito"), parse_citations(output), registry
    )

    assert source.entitlement is Entitlement.AGENT_DERIVED
    assert verification.spans[0].outcome is CheckOutcome.SOURCE_NOT_ENTITLED
