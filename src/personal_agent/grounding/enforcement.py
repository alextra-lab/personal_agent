"""D4 — block, retry with retrieval forced, then say so (ADR-0138 D4, FRE-1282).

An assertion span failing any gate blocks the turn and triggers a retry with retrieval
forced, bounded by a configured maximum attempt count. On exhausting the bound the terminal
state is an **explicit statement that no source was found, naming what was searched**.

**Why the loop terminates, which is the part an earlier ADR draft got wrong.** The terminal
statement is reachable because it consists entirely of **system-record** spans (D1's final
exempt region): what was searched, and that nothing was found. Their referent is this turn's
own record rather than the world, so they are not world-fact claims and cannot recurse into
another verification failure. The draft that argued this from *provenance* instead did not
hold — D1 would still have demanded a citation, and no retrieved source contains the
sentence "no source was found".

That construction has a direct consequence for this module: the terminal statement is
**built here, deterministically, from the turn record** — never generated. A generated
refusal would be model output like any other, would need verifying like any other, and the
loop would not be guaranteed to end.

**It is never a hedged guess.** A guess with a disclaimer is parametric knowledge wearing a
disclaimer, and under D2 it is not admissible. Stripping the claim silently is equally
rejected: silence is the disease being treated. So the statement names *what was searched*
and never *what the model was about to say* — repeating the blocked claim would deliver the
unsourced assertion in the very sentence that refuses it.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from personal_agent.grounding.verification import CheckOutcome, TurnVerification

MAX_LISTED_SEARCHES = 8
"""How many retrieval attempts the terminal statement names before summarising.

Bounded because the statement is delivered to a person: a turn that ran forty searches
should say so, not print forty lines.
"""


class TurnDecision(StrEnum):
    """What D4 says to do with one verified turn."""

    DELIVER = "deliver"
    RETRY_WITH_FORCED_RETRIEVAL = "retry_with_forced_retrieval"
    TERMINAL_NO_SOURCE = "terminal_no_source"


class EnforcementDecision(BaseModel):
    """The decision, with what a reader of the turn record needs to interpret it.

    Attributes:
        decision: What to do.
        attempt: Which generation attempt this verified, counting from 1.
        max_attempts: The configured bound.
        blocking_outcomes: The distinct gate outcomes that blocked the turn, so the reason
            for a retry — or a refusal — is legible without re-deriving it from the spans.
    """

    model_config = ConfigDict(frozen=True)

    decision: TurnDecision
    attempt: int
    max_attempts: int
    blocking_outcomes: tuple[CheckOutcome, ...] = ()


def decide(
    verification: TurnVerification, *, attempt: int, max_attempts: int
) -> EnforcementDecision:
    """Apply D4 to one turn's verification result.

    Args:
        verification: What the inline checks decided.
        attempt: Which generation attempt this is, counting from 1.
        max_attempts: The configured bound on generation attempts. A value of 1 means the
            first failure is terminal — a bound, not a disabled loop.

    Returns:
        The decision. A turn verification could not run on is **delivered**: a denied
        budget reservation or a broken extractor is a fact about Seshat's accounting, not
        evidence about the model's claim, and refusing the user's turn because our ledger
        ran dry punishes them for our bookkeeping. The turn is recorded as unverified —
        never as verified-and-passing — so a wave of these reads as the malfunction it is.
    """
    if not verification.available or verification.compliant:
        return EnforcementDecision(
            decision=TurnDecision.DELIVER, attempt=attempt, max_attempts=max_attempts
        )

    outcomes: tuple[CheckOutcome, ...] = ()
    for failure in verification.failures:
        if failure.outcome not in outcomes:
            outcomes = (*outcomes, failure.outcome)

    exhausted = attempt >= max_attempts
    return EnforcementDecision(
        decision=TurnDecision.TERMINAL_NO_SOURCE
        if exhausted
        else TurnDecision.RETRY_WITH_FORCED_RETRIEVAL,
        attempt=attempt,
        max_attempts=max_attempts,
        blocking_outcomes=outcomes,
    )


def build_retry_directive(verification: TurnVerification) -> str:
    """Return the instruction that forces retrieval on the next generation.

    Names each blocked span's *identifier and reason* — never re-states the claim as
    though it were established — so the model is told what failed without being handed its
    own unsourced assertion back as a premise.

    Args:
        verification: The failing verification.

    Returns:
        A directive for the retry's message list.
    """
    lines = [
        "Your previous answer was blocked: one or more assertions did not carry a "
        "citation that passed verification.",
        "",
    ]
    for failure in verification.failures:
        marker = failure.identifier or "no citation"
        lines.append(f"- [{marker}] {failure.outcome.value}: {failure.detail}")
    lines.extend(
        (
            "",
            "Retrieve a source before answering. Use the retrieval tools available to you, "
            "then cite each assertion with the identifier of the source that supports it. "
            "Do not restate the blocked assertions without a source, and do not hedge them "
            "— if no source supports a claim, leave the claim out and say what you searched.",
        )
    )
    return "\n".join(lines)


def build_no_source_statement(verification: TurnVerification, searched: Sequence[str]) -> str:
    """Return D4's terminal statement — the explicit no-source outcome.

    Built from the turn record, never generated. Every sentence is a **system-record** span
    under D1: what this turn searched, and that it found nothing that supports the answer.
    Those have the turn record as their referent rather than the world, which is what makes
    the statement reachable without recursing into another verification failure.

    Args:
        verification: The failing verification, for the count of blocked assertions.
        searched: What this turn actually retrieved or attempted to retrieve, in order.

    Returns:
        The statement. It names what was searched and never what was almost said: a
        refusal that repeats the blocked claim has delivered it.
    """
    blocked = len(verification.failures)
    noun = "assertion" if blocked == 1 else "assertions"
    lines = [
        f"I could not find a source for {blocked} {noun} in my answer, so I am not making "
        f"{'it' if blocked == 1 else 'them'}.",
    ]

    if searched:
        listed = list(searched[:MAX_LISTED_SEARCHES])
        lines.append("")
        lines.append("This turn searched:")
        lines.extend(f"- {entry}" for entry in listed)
        remaining = len(searched) - len(listed)
        if remaining > 0:
            lines.append(f"- and {remaining} further retrieval attempts")
    else:
        lines.append("")
        lines.append("This turn retrieved nothing: no search or fetch returned a usable source.")

    lines.append("")
    lines.append(
        "Tell me where to look — a URL, a document, or the fact itself — and I will work from that."
    )
    return "\n".join(lines)


__all__ = [
    "MAX_LISTED_SEARCHES",
    "EnforcementDecision",
    "TurnDecision",
    "build_no_source_statement",
    "build_retry_directive",
    "decide",
]
