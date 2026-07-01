"""Pure adjudication for living-knowledge Claim supersession (ADR-0098 D2, FRE-638).

Durable facts are first-class Claims, not frozen entity descriptions. When a new
Claim arrives that is *about the same thing* as an existing current Claim, one of
three things happens:

- **FRESH** — nothing current matches it; store it as the new current Claim.
- **SUPERSEDE** — it wins: invalidate the prior Claim (retained for history) and
  make the new one current. The *reason* annotates ADR-0098 D2's two modes —
  ``correction`` (a wrong fact, resolved by higher provenance/confidence) vs
  ``evolution`` (a fact that *was* true and changed, at equal confidence with a
  later observation). The reason is a heuristic audit label — the storage layer
  cannot robustly tell "I misremembered" from "it changed" from content alone;
  both modes produce the same graph outcome, which is all the ACs require.
- **REJECT** — it loses: a weaker (lower-confidence) or stale (out-of-order) claim
  must not clobber the current one (ADR-0098 D2: *not* naive last-write-wins). The
  losing claim is still retained as a non-current audit record by the writer.

"Same thing" is decided by content-embedding similarity (:func:`best_candidate`),
the honest generalization of ADR-0073's cross-fact slice: the extractor emits only
free-text ``content`` (no predicate to key on). The conservative threshold keeps
unrelated facts apart, and a false supersession is recoverable because the writer
always retains the superseded original.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from personal_agent.memory.embeddings import cosine_similarity

# Cosine floor for treating two Claims as the same fact-slot. Conservative so
# distinct-but-related facts do not collide; tunable (Codex #2, FRE-638).
CLAIM_MATCH_THRESHOLD = 0.83


class SupersessionAction(str, Enum):
    """The outcome of adjudicating a new Claim against the current one."""

    FRESH = "fresh"
    SUPERSEDE = "supersede"
    REJECT = "reject"


@dataclass(frozen=True)
class ClaimRecord:
    """A current Claim already in the graph, as a supersession candidate.

    Attributes:
        claim_id: The stored Claim's stable id (used as ``superseded_by`` back-pointer).
        content: The stored fact sentence (diagnostics/logging).
        confidence: The stored Claim's confidence, adjudicated against the new one.
        observed_at: The stored Claim's turn time — the bitemporal ordering axis.
        embedding: The stored Claim's content embedding, for similarity matching.
    """

    claim_id: str
    content: str
    confidence: float
    observed_at: datetime
    embedding: list[float]


@dataclass(frozen=True)
class Adjudication:
    """The decision for one incoming Claim.

    Attributes:
        action: FRESH / SUPERSEDE / REJECT.
        reason: ``"correction"`` or ``"evolution"`` when SUPERSEDE; else None.
    """

    action: SupersessionAction
    reason: str | None = None


def best_candidate(
    new_embedding: Sequence[float],
    candidates: Sequence[ClaimRecord],
    threshold: float = CLAIM_MATCH_THRESHOLD,
) -> ClaimRecord | None:
    """Return the current Claim most similar to the new one, if any clears ``threshold``.

    Args:
        new_embedding: The incoming Claim's content embedding.
        candidates: The owner's current Claims (``valid_to IS NULL``).
        threshold: Cosine floor for a match. Defaults to :data:`CLAIM_MATCH_THRESHOLD`.

    Returns:
        The highest-similarity candidate at or above ``threshold``, or None.
    """
    best: ClaimRecord | None = None
    best_sim = threshold
    new = list(new_embedding)
    for candidate in candidates:
        sim = cosine_similarity(new, candidate.embedding)
        if sim >= best_sim:
            best_sim = sim
            best = candidate
    return best


def adjudicate(
    *,
    new_confidence: float,
    new_observed_at: datetime,
    candidate: ClaimRecord | None,
) -> Adjudication:
    """Decide whether an incoming Claim is fresh, supersedes, or is rejected.

    Args:
        new_confidence: The incoming Claim's confidence.
        new_observed_at: The incoming Claim's turn time.
        candidate: The best-matching current Claim, or None if nothing matches.

    Returns:
        The :class:`Adjudication` (action + reason).
    """
    if candidate is None:
        return Adjudication(SupersessionAction.FRESH)
    # Weaker later claim must not clobber the current one (not naive last-write-wins).
    if new_confidence < candidate.confidence:
        return Adjudication(SupersessionAction.REJECT)
    # An older observation arriving after a newer current claim is stale.
    if new_observed_at < candidate.observed_at:
        return Adjudication(SupersessionAction.REJECT)
    reason = "correction" if new_confidence > candidate.confidence else "evolution"
    return Adjudication(SupersessionAction.SUPERSEDE, reason)
