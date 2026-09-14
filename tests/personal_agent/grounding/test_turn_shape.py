"""ADR-0151 D1 and D2 (FRE-1507): the turn shape and the settled/unsettled partition.

The shape tells the reader *why* a statement is unsourced, so each rule is pinned on the
inputs that separate it from a registry-count rule: a turn can make tool rounds or dispatch
workers and still offer the registry nothing.
"""

from __future__ import annotations

import pytest

from personal_agent.grounding.verification import (
    CheckOutcome,
    SpanVerification,
    TurnShape,
    TurnVerification,
    classify_turn_shape,
)

_UNSETTLED = {
    CheckOutcome.UNVERIFIABLE_BY_CONTAINMENT,
    CheckOutcome.ENTAILMENT_REQUIRED,
    CheckOutcome.ENTAILMENT_UNAVAILABLE,
}


def _span(outcome: CheckOutcome) -> SpanVerification:
    return SpanVerification(text="x", start=0, end=1, identifier=None, outcome=outcome)


_ONE_SPAN = TurnVerification(spans=(_span(CheckOutcome.UNCITED),))


@pytest.mark.parametrize(
    ("admitted", "rounds", "dispatched", "expected"),
    [
        (1, 1, 0, TurnShape.B),
        (1, 0, 2, TurnShape.B),
        (0, 0, 0, TurnShape.C),
        (0, 1, 0, TurnShape.A),
        (0, 0, 1, TurnShape.A),
        (0, 3, 2, TurnShape.A),
    ],
)
def test_d1_rules(admitted: int, rounds: int, dispatched: int, expected: TurnShape) -> None:
    shape = classify_turn_shape(
        _ONE_SPAN,
        tool_results_admitted=admitted,
        tool_rounds=rounds,
        sub_agents_dispatched=dispatched,
    )
    assert shape is expected


def test_no_non_exempt_statement_has_no_shape() -> None:
    shape = classify_turn_shape(
        TurnVerification(), tool_results_admitted=1, tool_rounds=1, sub_agents_dispatched=0
    )
    assert shape is None


def test_unavailable_verification_has_no_shape() -> None:
    shape = classify_turn_shape(
        TurnVerification(unavailable_reason="span extraction failed"),
        tool_results_admitted=0,
        tool_rounds=0,
        sub_agents_dispatched=0,
    )
    assert shape is None


@pytest.mark.parametrize("outcome", list(CheckOutcome))
def test_d2_partition_covers_every_outcome(outcome: CheckOutcome) -> None:
    """Every outcome is exactly one of passed, settled or unsettled."""
    verification = TurnVerification(spans=(_span(outcome),))

    settled = outcome is not CheckOutcome.PASSED and outcome not in _UNSETTLED
    assert (len(verification.settled_failures) == 1) is settled
    assert (len(verification.unverifiable) == 1) is (outcome in _UNSETTLED)
