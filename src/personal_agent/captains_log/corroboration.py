"""What a producer does with a read-before-emit result (ADR-0105 D9, FRE-1354).

ADR-0105 D9 added generation-time dedup: before a producer records a proposal it
reads sysgraph, and an equivalent idea that is already decided or still awaiting
suppresses the fresh row. That dedup is correct — it is why there are 28 live
proposals rather than hundreds of duplicates.

What was wrong is what happened to the entry *afterwards*. Every producer treated
``REINFORCED`` exactly like ``DECIDED_SKIP`` and discarded its ``proposed_change``.
Promotion builds its candidate list from ``proposed_change`` (``promotion.py``), so
an entry whose proposal had been stripped could never become a candidate — and
every sighting after the first is a reinforcement. The consequence was inverted:
the most corroborated signal the system had was the only one that could never
promote. A proposal seen 167 times sat unpromotable for eight weeks.

This module holds the one rule all six producers share, so the bar cannot drift
between them:

* ``DECIDED_SKIP`` — always suppress. The kind has a terminal outcome; re-promoting
  it would re-litigate a settled decision.
* ``REINFORCED`` at or above the bar — keep, and stamp the canonical identity.
  This is the corroboration ADR-0030's "min seen_count" criterion was always meant
  to evaluate.
* ``REINFORCED`` below the bar — suppress, unchanged. Loosening this would spend the
  ticket budget on noise, which is worse than promoting nothing.
* ``GENERATE_NEW`` / ``DEGRADED_GENERATE_NEW`` — keep, unchanged. Nothing equivalent
  exists, or sysgraph was unreachable and the check failed open.
"""

from __future__ import annotations

from personal_agent.captains_log.models import ProposedChange
from personal_agent.sysgraph.dedup import ReadBeforeEmitDecision, ReadBeforeEmitResult


def suppresses_proposal(result: ReadBeforeEmitResult, *, min_seen_count: int) -> bool:
    """Decide whether a read-before-emit result should erase the proposal.

    Args:
        result: The read-before-emit outcome for this sighting.
        min_seen_count: The promotion bar — the same
            ``settings.promotion_min_seen_count`` that
            :class:`~personal_agent.captains_log.promotion.PromotionCriteria` admits
            at, so a proposal is never kept here only to be dropped there (or the
            reverse).

    Returns:
        ``True`` when the caller must drop the proposal, ``False`` when it survives.
    """
    if result.decision is ReadBeforeEmitDecision.DECIDED_SKIP:
        return True
    if result.decision is ReadBeforeEmitDecision.REINFORCED:
        # `seen_count is None` means the repository did not report corroboration
        # (an older result shape, or a non-counting branch) — suppress, matching
        # the pre-FRE-1354 behaviour rather than promoting on an unknown count.
        return result.seen_count is None or result.seen_count < min_seen_count
    return False


def stamp_corroboration(
    proposed_change: ProposedChange, result: ReadBeforeEmitResult
) -> ProposedChange:
    """Return the proposal carrying the canonical row's identity and corroboration.

    Three fields come from sysgraph rather than from this sighting:

    ``seen_count``
        The authoritative post-increment count. This sighting only knows it saw the
        idea once; sysgraph knows the idea has been seen 167 times.
    ``fingerprint``
        The **canonical** identity of the ``(source, category, scope)`` group, not
        the hash of this sighting's wording. This is what makes every later sighting
        map to the same Linear ticket. On 2026-06-26 six sightings of one idea
        carried six different hashes and produced six tickets (FRE-623..628);
        carrying the canonical identity is what prevents that recurring.
    ``first_seen``
        The group's original creation time, so promotion's ``min_age_days`` measures
        the idea's real age rather than resetting on every sighting.

    Args:
        proposed_change: The proposal this sighting built.
        result: A ``REINFORCED`` read-before-emit result.

    Returns:
        A copy with the canonical fields applied. Content fields (``what``/``why``/
        ``how``) are never rewritten — only identity and corroboration are.
    """
    update: dict[str, object] = {}
    if result.seen_count is not None:
        update["seen_count"] = result.seen_count
    if result.fingerprint is not None:
        update["fingerprint"] = result.fingerprint
    if result.first_seen is not None:
        update["first_seen"] = result.first_seen
    if not update:
        return proposed_change
    return proposed_change.model_copy(update=update)


__all__ = ["stamp_corroboration", "suppresses_proposal"]
