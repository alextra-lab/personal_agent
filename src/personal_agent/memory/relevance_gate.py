"""The relevance predicate every reranker-gated admission path shares (ADR-0148 D4).

One rule for every path, parameterised by data -- the bound, the flag and the calibrated
component -- rather than one copy per path (FRE-1480). It lives in ``memory`` rather than
beside its first consumer in ``request_gateway/context.py`` because the proactive path
(FRE-1545) applies it inside ``memory/proactive.py``, and ``memory`` must not import the
gateway.
"""

from __future__ import annotations

from personal_agent.captains_log.turn_evidence import DropReason


def relevance_verdict(
    score: float | None,
    model: str | None,
    *,
    bound: float | None,
    gate_enabled: bool,
    calibrated_model: str | None,
) -> DropReason | None:
    """Decide whether a path's relevance gate rejects one item (ADR-0148 D4).

    Three conditions must all hold before a score is a relevance value at all, and the
    first two are not formalities:

    * A score exists. ``_rerank_fused_items`` leaves it None for a disabled reranker, a
      blank query, a one-item set, a raising call, an empty response, an index the
      response omitted, and the whole legacy single-path branch.
    * A model produced it. ``rerank()`` never raises and never returns empty -- it
      degrades to a passthrough whose "scores" are ``1 / (i + 1)``, rank order wearing
      the score field. Comparing that against a calibrated bound would decide admission
      on rank position, which is the defect this gate exists to remove.
    * That model is the one the bound was calibrated against. A primary outage falls back
      to a different reranker whose real scores sit on a different scale; FRE-695
      measured those scales as "arbitrary and not comparable across arms", so the bound
      says nothing about them. This case is the ordinary one on an outage, not an edge.

    Failing any of the three is reported as UNAVAILABLE rather than as a bound rejection:
    the path did not establish that the item is irrelevant, only that it cannot tell.
    Conflating the two would let a silently degraded reranker read as a corpus holding
    nothing relevant (FRE-1170).

    Args:
        score: The item's relevance score, or None when nothing scored it.
        model: The model that produced ``score``, or None when no model did.
        bound: The path's calibrated bound, or None when no calibration is in force.
        gate_enabled: Whether the path's gate is armed.
        calibrated_model: The reranker the configured bound was calibrated against, or
            None when no calibration is in force.

    Returns:
        The drop reason, or None when the item is admitted.
    """
    # A missing calibration leaves the gate inert and never defaults to zero (ADR-0148
    # D4). Checked before anything else, so an unconfigured deployment behaves exactly as
    # it did before this gate landed.
    if bound is None or not gate_enabled:
        return None
    if not isinstance(score, (int, float)) or isinstance(score, bool) or model is None:
        return DropReason.RECALL_RELEVANCE_UNAVAILABLE
    if calibrated_model is not None and model != calibrated_model:
        return DropReason.RECALL_RELEVANCE_UNAVAILABLE
    # Below the bound is rejected -- the `>=` convention the dense arm already uses in
    # memory/service.py, matched rather than fought (ADR-0148 D4).
    return None if float(score) >= bound else DropReason.RECALL_RELEVANCE_BOUND
