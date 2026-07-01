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

"Same thing" is decided facet-first, embedding-backstopped (FRE-712). The extractor
emits a normalized ``facet`` slot key per claim; matching treats it as **weighted
evidence, not a hard gate** (:func:`matching_candidates`): a shared facet lowers the
embedding bar (deterministic same-slot grouping), a differing facet raises it (only
near-identical content overrides LLM facet drift across turns), and an absent facet is
neutral (the FRE-638 base threshold, preserving legacy/no-facet claims). A false
supersession is recoverable because the writer always retains the superseded original.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from personal_agent.memory.embeddings import cosine_similarity

# Base cosine floor for treating two Claims as the same fact-slot when facet is
# absent on either side — exactly FRE-638 behavior. Tunable.
CLAIM_MATCH_THRESHOLD = 0.83
# When both facets are present and EQUAL, agreement lowers the embedding bar so the
# same slot groups even across rephrasings (FRE-712).
SAME_FACET_FLOOR = 0.60
# When both facets are present and DIFFER, disagreement raises the bar: only
# near-identical content merges, recovering from LLM facet drift without letting
# distinct-but-similar facts collide (Codex #2, FRE-712).
DIFF_FACET_FLOOR = 0.95

# The extractor's contradiction signal (FRE-712); off-vocabulary normalizes to "new".
_EXPLICIT_UPDATE_KINDS = frozenset({"correction", "evolution"})


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
        facet: The stored Claim's normalized slot key; "" for legacy/no-facet rows.
    """

    claim_id: str
    content: str
    confidence: float
    observed_at: datetime
    embedding: list[float]
    facet: str = ""


@dataclass(frozen=True)
class Adjudication:
    """The decision for one incoming Claim.

    Attributes:
        action: FRESH / SUPERSEDE / REJECT.
        reason: ``"correction"`` or ``"evolution"`` when SUPERSEDE; else None.
    """

    action: SupersessionAction
    reason: str | None = None


def _pair_threshold(new_facet: str, cand_facet: str) -> float:
    """The embedding floor for matching a new claim against one candidate (FRE-712).

    Facet is weighted evidence: agreement lowers the bar, disagreement raises it,
    absence on either side is neutral (the FRE-638 base).

    Args:
        new_facet: The incoming Claim's normalized facet ("" if none).
        cand_facet: The candidate Claim's normalized facet ("" if none).

    Returns:
        The cosine floor at or above which the pair is a slot match.
    """
    if new_facet and cand_facet:
        return SAME_FACET_FLOOR if new_facet == cand_facet else DIFF_FACET_FLOOR
    return CLAIM_MATCH_THRESHOLD


def matching_candidates(
    new_facet: str,
    new_embedding: Sequence[float],
    candidates: Sequence[ClaimRecord],
) -> list[ClaimRecord]:
    """Return every current Claim in the same fact-slot as the incoming one (FRE-712).

    A candidate matches when its cosine similarity clears the facet-weighted per-pair
    threshold (:func:`_pair_threshold`). Returning the whole set (not just the best)
    lets the writer invalidate all of them on supersede, so the "≤1 current per slot"
    invariant self-heals even if a prior bug left duplicates.

    Args:
        new_facet: The incoming Claim's normalized facet ("" if none).
        new_embedding: The incoming Claim's content embedding.
        candidates: The owner's current Claims (``valid_to IS NULL``).

    Returns:
        The matching current Claims (possibly empty).
    """
    new = list(new_embedding)
    return [
        candidate
        for candidate in candidates
        if cosine_similarity(new, candidate.embedding)
        >= _pair_threshold(new_facet, candidate.facet)
    ]


def strongest_blocker(matches: Sequence[ClaimRecord]) -> ClaimRecord | None:
    """Return the match the incoming Claim must out-rank to supersede (Codex #1).

    Adjudicating against the highest-confidence (ties broken by freshest) member of the
    matched set means a weaker new claim is rejected even when a *different*, lower-
    confidence claim also shares the slot — we never supersede past a stronger claim.

    Args:
        matches: The matching current Claims from :func:`matching_candidates`.

    Returns:
        The strongest blocker, or None when ``matches`` is empty.
    """
    if not matches:
        return None
    return max(matches, key=lambda c: (c.confidence, c.observed_at))


def adjudicate(
    *,
    new_confidence: float,
    new_observed_at: datetime,
    candidate: ClaimRecord | None,
    new_update_kind: str = "new",
) -> Adjudication:
    """Decide whether an incoming Claim is fresh, supersedes, or is rejected.

    Args:
        new_confidence: The incoming Claim's confidence.
        new_observed_at: The incoming Claim's turn time.
        candidate: The strongest matching current Claim, or None if nothing matches.
        new_update_kind: The extractor's contradiction signal — "correction"/
            "evolution" drives the SUPERSEDE *reason* directly; "new"/off-vocabulary
            falls back to the FRE-638 confidence-delta heuristic. The signal drives
            the label only, never the FRESH/REJECT safety decision (ADR-0098 D2).

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
    # Prefer the extractor's explicit signal for the label; else the FRE-638 heuristic.
    if new_update_kind in _EXPLICIT_UPDATE_KINDS:
        reason = new_update_kind
    else:
        reason = "correction" if new_confidence > candidate.confidence else "evolution"
    return Adjudication(SupersessionAction.SUPERSEDE, reason)
