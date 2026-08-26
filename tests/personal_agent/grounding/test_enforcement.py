"""D4 — block, retry, then say so (ADR-0138 D4) — FRE-1282 AC-5.

AC-5 fails if the loop exceeds its bound, emits a hedged guess or a named candidate,
silently strips the claim, or the terminal statement itself fails verification. Each of
those is asserted here, and the loop is driven to exhaustion rather than reasoned about.
"""

from __future__ import annotations

from personal_agent.grounding.citations import parse_citations
from personal_agent.grounding.enforcement import (
    TurnDecision,
    build_no_source_statement,
    build_retry_directive,
    decide,
)
from personal_agent.grounding.source_registry import SourceRegistry
from personal_agent.grounding.spans import (
    NonExemptReason,
    Span,
    SpanExtraction,
    SpanLabel,
)
from personal_agent.grounding.verification import CheckOutcome, unavailable, verify_turn

TURN = "trace-enforce-0001"
CLAIM = "Paris has 2.1 million residents"


def _verified(output: str, registry: SourceRegistry):
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


def _failing():
    """A verification that cannot pass — the unsatisfiable-retrieval condition AC-5 forces."""
    return _verified(f"{CLAIM}.", SourceRegistry(turn_id=TURN))


# ── AC-5 — the bound ────────────────────────────────────────────────────────────────


def test_loop_terminates_at_exactly_the_bound() -> None:
    """Force unsatisfiable retrieval and drive the loop; it must end, on the bound.

    Driven rather than asserted about: a bound that is only reasoned about is a bound that
    has never been shown to hold.
    """
    verification = _failing()
    decisions = []
    for attempt in range(1, 6):
        decision = decide(verification, attempt=attempt, max_attempts=3)
        decisions.append(decision.decision)
        if decision.decision is not TurnDecision.RETRY_WITH_FORCED_RETRIEVAL:
            break

    assert decisions == [
        TurnDecision.RETRY_WITH_FORCED_RETRIEVAL,
        TurnDecision.RETRY_WITH_FORCED_RETRIEVAL,
        TurnDecision.TERMINAL_NO_SOURCE,
    ]


def test_a_bound_of_one_makes_the_first_failure_terminal() -> None:
    """A bound is a bound at its smallest value, not a disabled loop."""
    assert decide(_failing(), attempt=1, max_attempts=1).decision is TurnDecision.TERMINAL_NO_SOURCE


def test_a_passing_turn_is_delivered_unchanged() -> None:
    """The paired positive: D4 must not fire on a turn that verified."""
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents within the city limits.",
    )
    assert registration.source is not None
    verification = _verified(f"{CLAIM} [{registration.source.identifier}].", registry)

    decision = decide(verification, attempt=1, max_attempts=3)

    assert decision.decision is TurnDecision.DELIVER
    assert decision.blocking_outcomes == ()


def test_a_turn_verification_could_not_run_on_is_delivered_not_refused() -> None:
    """A denied budget is our accounting, not evidence about the claim."""
    decision = decide(unavailable("budget denied"), attempt=1, max_attempts=3)

    assert decision.decision is TurnDecision.DELIVER


def test_the_blocking_reason_survives_into_the_decision() -> None:
    """A retry whose reason is not legible cannot be diagnosed when it recurs."""
    decision = decide(_failing(), attempt=1, max_attempts=3)

    assert decision.blocking_outcomes == (CheckOutcome.UNCITED,)


# ── AC-5 — the terminal state ───────────────────────────────────────────────────────


def test_terminal_statement_names_what_was_searched() -> None:
    """D4: "an explicit statement that no source was found, naming what was searched"."""
    statement = build_no_source_statement(
        _failing(), ["web_search(paris population)", "fetch_url(https://example.com/paris)"]
    )

    assert "could not find a source" in statement
    assert "web_search(paris population)" in statement
    assert "fetch_url(https://example.com/paris)" in statement


def test_terminal_statement_says_so_when_nothing_was_retrieved() -> None:
    """Naming *no* searches is still naming what was searched — silence is not."""
    statement = build_no_source_statement(_failing(), [])

    assert "retrieved nothing" in statement


def test_terminal_statement_carries_no_hedge_and_no_named_candidate() -> None:
    """AC-5 — a guess with a disclaimer is parametric knowledge wearing a disclaimer.

    The blocked claim's own text must not appear: a refusal that repeats the assertion has
    delivered it, which is the failure mode dressed as the remedy.
    """
    statement = build_no_source_statement(_failing(), ["web_search(paris population)"])

    assert CLAIM not in statement
    assert "2.1 million" not in statement
    for hedge in ("probably", "likely", "I believe", "as far as I know", "roughly", "might be"):
        assert hedge.lower() not in statement.lower()


def test_the_claim_is_never_silently_stripped() -> None:
    """AC-5 — silence is the disease being treated, so the refusal must be explicit."""
    statement = build_no_source_statement(_failing(), [])

    assert statement.strip() != ""
    assert "not making" in statement


def test_terminal_statement_is_built_not_generated() -> None:
    """The property that guarantees termination: it is a function of the turn record.

    Deterministic output for the same record means it is not model output, so it is not
    subject to verification and cannot recurse into another failure.
    """
    verification = _failing()
    searched = ["web_search(paris population)"]

    assert build_no_source_statement(verification, searched) == build_no_source_statement(
        verification, searched
    )


def test_terminal_statement_bounds_how_many_searches_it_lists() -> None:
    """It is delivered to a person, so forty searches are summarised rather than printed."""
    statement = build_no_source_statement(_failing(), [f"web_search(q{i})" for i in range(40)])

    assert "further retrieval attempts" in statement
    assert statement.count("web_search(") <= 8


# ── The retry directive ─────────────────────────────────────────────────────────────


def test_retry_directive_forces_retrieval_without_restating_the_claim() -> None:
    """Handing the model its own blocked assertion back would make it a premise."""
    directive = build_retry_directive(_failing())

    assert "Retrieve a source before answering" in directive
    assert CheckOutcome.UNCITED.value in directive
    assert CLAIM not in directive
