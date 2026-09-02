"""Turn evidence classification (ADR-0139 D1, AC-1 and AC-2, FRE-1332).

``passed_count: 0`` is equally consistent with a careless model and with a system that
offered nothing citable. ``classify_turn_evidence`` is the denominator that tells the two
apart, mechanically: non-exempt spans exist, at least one tool result was offered, and
none was admitted.
"""

from __future__ import annotations

from personal_agent.grounding.verification import (
    CheckOutcome,
    SpanVerification,
    TurnEvidenceClass,
    TurnVerification,
    classify_turn_evidence,
)

CLAIM = "Paris has 2.1 million residents"


def _span(outcome: CheckOutcome) -> SpanVerification:
    return SpanVerification(
        text=CLAIM,
        start=0,
        end=len(CLAIM),
        identifier=None,
        outcome=outcome,
    )


def test_no_non_exempt_spans_is_no_assertions() -> None:
    verification = TurnVerification(spans=())

    result = classify_turn_evidence(verification, tool_results_offered=0, tool_results_admitted=0)

    assert result is TurnEvidenceClass.NO_ASSERTIONS


def test_no_non_exempt_spans_is_no_assertions_even_with_tool_activity() -> None:
    """Exempt-only output stays ``no_assertions`` regardless of what the registry saw."""
    verification = TurnVerification(spans=())

    result = classify_turn_evidence(verification, tool_results_offered=3, tool_results_admitted=0)

    assert result is TurnEvidenceClass.NO_ASSERTIONS


def test_ac1_every_tool_result_refused_is_uncitable() -> None:
    """AC-1: non-exempt spans exist, a tool result was offered, none was admitted."""
    verification = TurnVerification(spans=(_span(CheckOutcome.UNCITED),))

    result = classify_turn_evidence(verification, tool_results_offered=1, tool_results_admitted=0)

    assert result is TurnEvidenceClass.UNCITABLE


def test_ac2_a_weights_only_turn_is_citable_not_uncitable() -> None:
    """AC-2: a turn that called no tools and asserted anyway stays in the denominator."""
    verification = TurnVerification(spans=(_span(CheckOutcome.UNCITED),))

    result = classify_turn_evidence(verification, tool_results_offered=0, tool_results_admitted=0)

    assert result is TurnEvidenceClass.CITABLE


def test_an_admitted_tool_result_is_citable() -> None:
    verification = TurnVerification(spans=(_span(CheckOutcome.PASSED),))

    result = classify_turn_evidence(verification, tool_results_offered=1, tool_results_admitted=1)

    assert result is TurnEvidenceClass.CITABLE


def test_a_partially_admitted_turn_is_citable() -> None:
    """One admitted result among several offered still counts as citable overall."""
    verification = TurnVerification(spans=(_span(CheckOutcome.UNCITED),))

    result = classify_turn_evidence(verification, tool_results_offered=2, tool_results_admitted=1)

    assert result is TurnEvidenceClass.CITABLE
