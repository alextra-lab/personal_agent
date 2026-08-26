"""FRE-1303: end-to-end proof that recall_personal_history cannot cite the agent to itself.

Mirrors test_search_memory_entitlement_e2e.py (FRE-1302, the sibling pull-path fix): a synthetic
Turn row in the exact shape ``recall_personal_history_executor`` emits
(``tools/personal_history.py:186-198``) is carried into the real registry and the real verifier,
proving the whole path denies the model's own prior reply rather than registering it at
``EXTERNAL`` — the most-trusted tier the contract has, and the one this tool held before the fix.

The claim texts name an entity and state a figure so D3(c) containment resolves directly and
D3(d) inline entailment never engages — that check is FRE-1286's own surface, not this test's.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from personal_agent.grounding.citations import parse_citations
from personal_agent.grounding.source_registry import Entitlement, SourceRegistry
from personal_agent.grounding.spans import (
    ExemptRegion,
    NonExemptReason,
    Span,
    SpanExtraction,
    SpanLabel,
)
from personal_agent.grounding.verification import CheckOutcome, verify_turn

TURN = "trace-fre1303-e2e"


def _turn_row(
    *,
    user_message: str,
    assistant_response: str = "",
    summary: str = "",
    entities: Sequence[str] = (),
) -> dict[str, object]:
    """One element of the ``turns`` list, exactly as the executor builds it.

    ``assistant_response``, ``summary`` and ``entities`` default to the empty shapes the
    executor's own ``or ""`` / ``or []`` fallbacks produce, so a caller opts *in* to each
    agent-authored field rather than having to strip it.
    """
    return {
        "turn_id": "turn-8801",
        "timestamp": "2026-08-19T10:00:00+00:00",
        "session_id": "session-4412",
        "user_message": user_message,
        "assistant_response": assistant_response,
        "summary": summary,
        "entities": list(entities),
    }


def _recall_output(turns: list[dict[str, object]]) -> str:
    """The exact top-level shape recall_personal_history_executor returns."""
    return json.dumps(
        {
            "turns": turns,
            "total": len(turns),
            "window_days": 7,
            "user_id": "user-1",
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


def _run_chain(*, turns: list[dict[str, object]], cited: str) -> tuple[Entitlement, CheckOutcome]:
    """Turn rows -> recall_personal_history JSON -> register_tool_result -> verify_turn.

    ``days_ago`` is numeric, so ``_strip_argument_echo`` has no eligible value and the
    registered content is the emitted JSON byte for byte.
    """
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="recall_personal_history",
        arguments={"days_ago": 7},
        content=_recall_output(turns),
    )
    assert registration.source is not None
    source = registration.source

    output = f"{cited} [{source.identifier}]."
    verification = verify_turn(_non_exempt(output, cited), parse_citations(output), registry)
    return source.entitlement, verification.spans[0].outcome


# ── AC-1 — the model cannot cite its own prior reply at a trusted tier ──────────────


def test_assistant_response_recall_is_refused() -> None:
    """The seeded negative this ticket exists for.

    Before the fix ``register_tool_result`` hardcoded ``EXTERNAL`` for every
    ``TYPED_RETRIEVAL_TOOLS`` member but ``search_memory``, so the model's own sentence from
    a previous week came back as the *most*-trusted source the contract has and passed.
    """
    entitlement, outcome = _run_chain(
        turns=[
            _turn_row(
                user_message="what did that tin cost?",
                assistant_response="Ortiz bonito costs 12 euros",
            )
        ],
        cited="Ortiz bonito costs 12 euros",
    )

    assert entitlement is Entitlement.AGENT_DERIVED
    assert outcome is CheckOutcome.SOURCE_NOT_ENTITLED


def test_agent_written_summary_is_refused() -> None:
    """``Turn.summary`` is generated, not stated — the same authorship as the response."""
    entitlement, outcome = _run_chain(
        turns=[
            _turn_row(
                user_message="what did that tin cost?",
                summary="Ortiz bonito costs 12 euros",
            )
        ],
        cited="Ortiz bonito costs 12 euros",
    )

    assert entitlement is Entitlement.AGENT_DERIVED
    assert outcome is CheckOutcome.SOURCE_NOT_ENTITLED


def test_extracted_entities_are_refused() -> None:
    """Entity names come from extraction (ADR-0098), so they are the agent's words too."""
    entitlement, outcome = _run_chain(
        turns=[
            _turn_row(
                user_message="what did that tin cost?",
                entities=["Ortiz bonito costs 12 euros"],
            )
        ],
        cited="Ortiz bonito costs 12 euros",
    )

    assert entitlement is Entitlement.AGENT_DERIVED
    assert outcome is CheckOutcome.SOURCE_NOT_ENTITLED


def test_one_agent_authored_turn_denies_the_whole_call() -> None:
    """Most-restrictive aggregation: FRE-1280 registers one source per tool call, so a call
    is only as entitled as its least-entitled turn — the same rule
    ``_search_memory_entitlement`` applies across Claim rows.
    """
    entitlement, outcome = _run_chain(
        turns=[
            _turn_row(user_message="Ortiz bonito costs 12 euros"),
            _turn_row(user_message="and the anchovies?", assistant_response="Nine euros a tin."),
        ],
        cited="Ortiz bonito costs 12 euros",
    )

    assert entitlement is Entitlement.AGENT_DERIVED
    assert outcome is CheckOutcome.SOURCE_NOT_ENTITLED


# ── AC-2 — the owner's own words stay usable ────────────────────────────────────────


def test_user_message_only_recall_is_citable() -> None:
    """The registry-level positive control: ``Turn.user_message`` *is* the owner's words.

    A blanket tool-keyed denial would fail here, which is exactly what AC-2 forbids.
    """
    entitlement, outcome = _run_chain(
        turns=[_turn_row(user_message="Ortiz bonito costs 12 euros")],
        cited="Ortiz bonito costs 12 euros",
    )

    assert entitlement is Entitlement.USER_STATED
    assert outcome is CheckOutcome.PASSED


def test_attributed_restatement_never_reaches_the_entitlement_gate() -> None:
    """The half of AC-2 that carries the practical load.

    Real turns almost always carry an ``assistant_response``, so the registry-level control
    above is the narrow case. D1 is the broad one: a span that attributedly restates the
    user's words is ``CLAIM_EXEMPT``, and ``verify_turn`` iterates ``extraction.non_exempt``
    — such a span is never verified at all, whatever this call's entitlement turns out to be.
    """
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="recall_personal_history",
        arguments={"days_ago": 7},
        content=_recall_output(
            [
                _turn_row(
                    user_message="Ortiz bonito costs 12 euros",
                    assistant_response="Noted — I'll remember that.",
                )
            ]
        ),
    )
    assert registration.source is not None
    assert registration.source.entitlement is Entitlement.AGENT_DERIVED

    output = "You told me Ortiz bonito costs 12 euros."
    text = "You told me Ortiz bonito costs 12 euros."
    extraction = SpanExtraction(
        output=output,
        spans=(
            Span(
                start=0,
                end=len(text),
                text=text,
                label=SpanLabel.CLAIM_EXEMPT,
                region=ExemptRegion.ATTRIBUTED_RESTATEMENT,
            ),
        ),
    )

    verification = verify_turn(extraction, parse_citations(output), registry)

    assert verification.spans == ()


# ── Fail direction ──────────────────────────────────────────────────────────────────


def test_unparsable_recall_content_denies() -> None:
    """Any shape the rule does not fully understand denies, never readmits.

    ``EXTERNAL`` is an *admitted* tier, so falling back to it on a malformed result would
    readmit everything the parse could not account for — the same direction
    ``_search_memory_entitlement`` and ``_entitlement_of`` already document.
    """
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="recall_personal_history",
        arguments={"days_ago": 7},
        content="Ortiz bonito costs 12 euros",
    )

    assert registration.source is not None
    assert registration.source.entitlement is Entitlement.AGENT_DERIVED


def test_turns_holding_a_non_mapping_denies() -> None:
    """A ``turns`` list whose members are not mappings is a shape this rule cannot read."""
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="recall_personal_history",
        arguments={"days_ago": 7},
        content=json.dumps({"turns": ["Ortiz bonito costs 12 euros"], "total": 1}),
    )

    assert registration.source is not None
    assert registration.source.entitlement is Entitlement.AGENT_DERIVED


def test_empty_window_is_not_promoted_to_external() -> None:
    """A window that matched nothing supports no claim, so it must not sit at EXTERNAL.

    Distinct from ``search_memory``'s empty case, which keeps ``EXTERNAL`` because its
    non-Claim payload (turns, entities) is still external-ish. Here the *only* payload is
    turns, so an empty result has no user-stated content to be entitled to either.
    """
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="recall_personal_history",
        arguments={"days_ago": 7},
        content=_recall_output([]),
    )

    assert registration.source is not None
    assert registration.source.entitlement is Entitlement.AGENT_DERIVED
