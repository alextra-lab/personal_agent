"""D3/D4 as amended by ADR-0151 — one cite-only retry on shape B, and no refusal (FRE-1509).

``decide()`` reads the turn shape and the settled/unsettled partition FRE-1507 built. A
retry happens only on a shape B turn with a settled failure, on its first attempt. Every
other turn delivers its last generation, and the declaration says what was not grounded.
"""

from __future__ import annotations

import pytest

import personal_agent.grounding.enforcement as enforcement
from personal_agent.grounding.citations import parse_citations
from personal_agent.grounding.enforcement import TurnDecision, build_retry_directive, decide
from personal_agent.grounding.source_registry import SourceRegistry
from personal_agent.grounding.spans import (
    NonExemptReason,
    Span,
    SpanExtraction,
    SpanLabel,
)
from personal_agent.grounding.verification import (
    CheckOutcome,
    SpanVerification,
    TurnShape,
    TurnVerification,
    unavailable,
    verify_turn,
)

TURN = "trace-enforce-0001"
CLAIM = "Paris has 2.1 million residents"


def _verified(output: str, registry: SourceRegistry) -> TurnVerification:
    """Verify a one-span output the way the turn path does."""
    start = output.index(CLAIM)
    extraction = SpanExtraction(
        output=output,
        spans=(
            Span(
                start=start,
                end=start + len(CLAIM),
                text=CLAIM,
                label=SpanLabel.CLAIM_NON_EXEMPT,
                reason=NonExemptReason.CLASSIFIED,
            ),
        ),
    )
    return verify_turn(extraction, parse_citations(output), registry)


def _failing() -> TurnVerification:
    """One uncited statement: a settled failure."""
    return _verified(f"{CLAIM}.", SourceRegistry(turn_id=TURN))


def _spans(*outcomes: CheckOutcome) -> TurnVerification:
    return TurnVerification(
        spans=tuple(
            SpanVerification(text=f"s{i}", start=0, end=2, identifier=None, outcome=outcome)
            for i, outcome in enumerate(outcomes)
        )
    )


# ── AC-1 — an unsettled failure never retries ───────────────────────────────────────


def test_ac1_an_unsettled_only_shape_b_turn_delivers() -> None:
    """A limit of the verifier is not evidence about the model's statement."""
    verification = _spans(
        CheckOutcome.UNVERIFIABLE_BY_CONTAINMENT, CheckOutcome.ENTAILMENT_UNAVAILABLE
    )

    decision = decide(verification, shape=TurnShape.B, attempt=1, max_attempts=2)

    assert decision.decision is TurnDecision.DELIVER
    assert decision.blocking_outcomes == ()


def test_blocking_outcomes_list_settled_outcomes_only() -> None:
    verification = _spans(
        CheckOutcome.UNCITED,
        CheckOutcome.UNVERIFIABLE_BY_CONTAINMENT,
        CheckOutcome.NOT_CONTAINED,
        CheckOutcome.UNCITED,
    )

    decision = decide(verification, shape=TurnShape.B, attempt=1, max_attempts=2)

    assert decision.decision is TurnDecision.RETRY_CITE_ONLY
    assert decision.blocking_outcomes == (CheckOutcome.UNCITED, CheckOutcome.NOT_CONTAINED)


# ── AC-2 — only shape B retries, and only once ──────────────────────────────────────


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        (TurnShape.A, TurnDecision.DELIVER),
        (TurnShape.B, TurnDecision.RETRY_CITE_ONLY),
        (TurnShape.C, TurnDecision.DELIVER),
        (None, TurnDecision.DELIVER),
    ],
)
def test_ac2_only_shape_b_retries(shape: TurnShape | None, expected: TurnDecision) -> None:
    """Retrieval cannot supply a command's output, and C has nothing to cite from."""
    decision = decide(_failing(), shape=shape, attempt=1, max_attempts=2)

    assert decision.decision is expected
    if expected is TurnDecision.DELIVER:
        assert decision.blocking_outcomes == ()


def test_shape_b_retries_at_most_once() -> None:
    """The second generation is delivered whatever it verifies as."""
    decision = decide(_failing(), shape=TurnShape.B, attempt=2, max_attempts=2)

    assert decision.decision is TurnDecision.DELIVER


def test_a_cap_of_one_never_retries() -> None:
    decision = decide(_failing(), shape=TurnShape.B, attempt=1, max_attempts=1)

    assert decision.decision is TurnDecision.DELIVER


def test_a_passing_turn_is_delivered() -> None:
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents within the city limits.",
    )
    assert registration.source is not None
    verification = _verified(f"{CLAIM} [{registration.source.identifier}].", registry)

    decision = decide(verification, shape=TurnShape.B, attempt=1, max_attempts=2)

    assert decision.decision is TurnDecision.DELIVER
    assert decision.blocking_outcomes == ()


def test_a_turn_verification_could_not_run_on_is_delivered() -> None:
    """A denied budget is our accounting, not evidence about the claim."""
    decision = decide(unavailable("budget denied"), shape=None, attempt=1, max_attempts=2)

    assert decision.decision is TurnDecision.DELIVER


# ── AC-4 — the refusal is withdrawn ─────────────────────────────────────────────────


def test_ac4_terminal_no_source_is_withdrawn() -> None:
    assert {d.value for d in TurnDecision} == {"deliver", "retry_cite_only"}
    assert not hasattr(enforcement, "build_no_source_statement")


# ── D3 — the retry directive cites, it does not retrieve ────────────────────────────


def test_retry_directive_cites_only() -> None:
    """Names what failed, never the claim, and never asks for retrieval."""
    directive = build_retry_directive(_failing())

    assert CheckOutcome.UNCITED.value in directive
    assert CLAIM not in directive
    assert "cite" in directive.lower()
    assert "retriev" not in directive.lower()
    assert "search" not in directive.lower()


def test_retry_directive_names_settled_failures_only() -> None:
    """An unsettled statement is not the model's to fix, so it is not listed."""
    directive = build_retry_directive(
        _spans(CheckOutcome.UNCITED, CheckOutcome.UNVERIFIABLE_BY_CONTAINMENT)
    )

    assert CheckOutcome.UNCITED.value in directive
    assert CheckOutcome.UNVERIFIABLE_BY_CONTAINMENT.value not in directive
