"""D4 as amended by ADR-0151 — one cite-only retry on shape B, then deliver (FRE-1509).

ADR-0138 D4 blocked every failing turn, forced retrieval, retried, and at the bound
replaced the answer with a refusal. That made a turn which learned its facts by running a
command unanswerable, because retrieval cannot supply what the command produced
(FRE-1328). ADR-0151 narrows the remedy to the turns a retry can repair:

- **Shape B only.** The registry already holds a tool source from this turn, so the model
  can cite what it has. Shapes A and C have nothing a retry could cite, and never retry.
- **Settled failures only** (D2). A statement the verifier could not settle is a limit of
  the verifier, not evidence about the statement, so it never triggers a retry.
- **One retry, and it cites.** The directive names what failed and tells the model to cite
  from the sources already in the conversation or leave the statement out. The request
  that carries it offers no tool call, so it cannot retrieve.
- **No refusal.** After the retry, or with no retry, the turn delivers its last
  generation. The per-shape declaration (FRE-1507) tells the reader what was not grounded.

The directive names each blocked span's identifier and reason, never its text: handing the
model its own unsourced assertion back would make it a premise.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from personal_agent.grounding.verification import CheckOutcome, TurnShape, TurnVerification


class TurnDecision(StrEnum):
    """What D4 says to do with one verified turn."""

    DELIVER = "deliver"
    RETRY_CITE_ONLY = "retry_cite_only"


class EnforcementDecision(BaseModel):
    """The decision, with what a reader of the turn record needs to interpret it.

    Attributes:
        decision: What to do.
        attempt: Which generation attempt this verified, counting from 1.
        max_attempts: The configured bound.
        blocking_outcomes: The distinct settled outcomes that ordered a retry, so the reason
            is legible without re-deriving it from the spans. Empty on every delivery,
            because nothing blocked it.
    """

    model_config = ConfigDict(frozen=True)

    decision: TurnDecision
    attempt: int
    max_attempts: int
    blocking_outcomes: tuple[CheckOutcome, ...] = ()


def decide(
    verification: TurnVerification,
    *,
    shape: TurnShape | None,
    attempt: int,
    max_attempts: int,
) -> EnforcementDecision:
    """Apply D4, as amended by ADR-0151 D3, to one turn's verification result.

    Args:
        verification: What the inline checks decided.
        shape: This turn's shape (ADR-0151 D1). None when verification did not run or the
            turn has no non-exempt statement, which always delivers.
        attempt: Which generation attempt this is, counting from 1.
        max_attempts: The configured bound on generation attempts, 1 or 2. 1 means no
            retry.

    Returns:
        ``RETRY_CITE_ONLY`` for a shape B turn with one or more settled failures before the
        bound, otherwise ``DELIVER``. A turn verification could not run on is delivered: a
        denied budget reservation is a fact about Seshat's accounting, not evidence about
        the model's claim.
    """
    if shape is not TurnShape.B or attempt >= max_attempts:
        return EnforcementDecision(
            decision=TurnDecision.DELIVER, attempt=attempt, max_attempts=max_attempts
        )

    outcomes: tuple[CheckOutcome, ...] = ()
    for failure in verification.settled_failures:
        if failure.outcome not in outcomes:
            outcomes = (*outcomes, failure.outcome)

    if not outcomes:
        return EnforcementDecision(
            decision=TurnDecision.DELIVER, attempt=attempt, max_attempts=max_attempts
        )
    return EnforcementDecision(
        decision=TurnDecision.RETRY_CITE_ONLY,
        attempt=attempt,
        max_attempts=max_attempts,
        blocking_outcomes=outcomes,
    )


def build_retry_directive(verification: TurnVerification) -> str:
    """Return the instruction for the cite-only retry (ADR-0151 D3).

    Lists the settled failures by identifier and outcome. Unsettled statements are not
    listed: the verifier could not decide them, so they are not the model's to repair.

    Args:
        verification: The failing verification.

    Returns:
        A directive for the retry's message list.
    """
    lines = [
        "Your previous answer was blocked: one or more statements did not carry a "
        "citation that passed verification.",
        "",
    ]
    for failure in verification.settled_failures:
        marker = failure.identifier or "no citation"
        lines.append(f"- [{marker}] {failure.outcome.value}: {failure.detail}")
    lines.extend(
        (
            "",
            "Write your answer again from the sources already in this conversation. Cite "
            "each statement with the identifier of the source that supports it. No tool "
            "is available for this reply. If no source in this conversation supports a "
            "statement, leave the statement out. Do not restate a blocked statement "
            "without a citation, and do not hedge it.",
        )
    )
    return "\n".join(lines)


__all__ = [
    "EnforcementDecision",
    "TurnDecision",
    "build_retry_directive",
    "decide",
]
