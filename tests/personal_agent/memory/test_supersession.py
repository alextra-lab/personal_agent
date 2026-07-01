"""Unit tests for the pure Claim supersession adjudication (FRE-638 + FRE-712).

FRE-638 established the correction/evolution/reject decision on content-embedding
similarity. FRE-712 makes matching facet-aware (facet is weighted evidence, not a
hard gate) and lets the extractor's ``update_kind`` drive the correction-vs-evolution
label. All logic here is pure — no Neo4j, no embedder.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from personal_agent.memory.supersession import (
    CLAIM_MATCH_THRESHOLD,
    DIFF_FACET_FLOOR,
    SAME_FACET_FLOOR,
    ClaimRecord,
    SupersessionAction,
    adjudicate,
    matching_candidates,
    strongest_blocker,
)

_T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(days=90)


def _vec(cos: float) -> list[float]:
    """A unit vector whose cosine with [1, 0] is exactly ``cos``."""
    return [cos, math.sqrt(max(0.0, 1.0 - cos * cos))]


def _cand(
    *,
    confidence: float = 0.8,
    observed_at: datetime = _T0,
    embedding: list[float] | None = None,
    facet: str = "",
    claim_id: str = "cand",
) -> ClaimRecord:
    return ClaimRecord(
        claim_id=claim_id,
        content="candidate",
        confidence=confidence,
        observed_at=observed_at,
        embedding=embedding if embedding is not None else _vec(1.0),
        facet=facet,
    )


# --- adjudicate: FRE-638 behavior preserved (update_kind defaults to "new") -----


def test_no_candidate_is_fresh() -> None:
    assert adjudicate(new_confidence=0.8, new_observed_at=_T1, candidate=None).action is (
        SupersessionAction.FRESH
    )


def test_equal_confidence_newer_time_is_evolution_by_heuristic() -> None:
    result = adjudicate(new_confidence=0.8, new_observed_at=_T1, candidate=_cand(confidence=0.8))
    assert result.action is SupersessionAction.SUPERSEDE
    assert result.reason == "evolution"


def test_higher_confidence_is_correction_by_heuristic() -> None:
    result = adjudicate(new_confidence=0.8, new_observed_at=_T1, candidate=_cand(confidence=0.5))
    assert result.action is SupersessionAction.SUPERSEDE
    assert result.reason == "correction"


def test_lower_confidence_is_rejected() -> None:
    result = adjudicate(new_confidence=0.5, new_observed_at=_T1, candidate=_cand(confidence=0.8))
    assert result.action is SupersessionAction.REJECT


def test_stale_out_of_order_claim_is_rejected() -> None:
    result = adjudicate(
        new_confidence=0.9, new_observed_at=_T0, candidate=_cand(confidence=0.8, observed_at=_T1)
    )
    assert result.action is SupersessionAction.REJECT


# --- adjudicate: FRE-712 explicit update_kind drives the label ------------------


def test_explicit_correction_overrides_evolution_heuristic() -> None:
    # Equal confidence + newer time → heuristic would say 'evolution'; signal says 'correction'.
    result = adjudicate(
        new_confidence=0.8,
        new_observed_at=_T1,
        candidate=_cand(confidence=0.8),
        new_update_kind="correction",
    )
    assert result.action is SupersessionAction.SUPERSEDE
    assert result.reason == "correction"


def test_explicit_evolution_overrides_correction_heuristic() -> None:
    # Higher confidence → heuristic would say 'correction'; signal says 'evolution'.
    result = adjudicate(
        new_confidence=0.9,
        new_observed_at=_T1,
        candidate=_cand(confidence=0.5),
        new_update_kind="evolution",
    )
    assert result.action is SupersessionAction.SUPERSEDE
    assert result.reason == "evolution"


def test_explicit_signal_does_not_override_reject_safety() -> None:
    # A 'correction' signal must NOT force a supersede past a stronger claim.
    result = adjudicate(
        new_confidence=0.4,
        new_observed_at=_T1,
        candidate=_cand(confidence=0.8),
        new_update_kind="correction",
    )
    assert result.action is SupersessionAction.REJECT


# --- matching_candidates: facet as weighted evidence ---------------------------


def test_same_facet_matches_at_modest_similarity() -> None:
    cand = _cand(facet="lease_end_date", embedding=_vec(0.70))
    matched = matching_candidates("lease_end_date", _vec(1.0), [cand])
    assert matched == [cand]  # 0.70 >= SAME_FACET_FLOOR (0.60)


def test_same_facet_below_floor_does_not_match() -> None:
    cand = _cand(facet="lease_end_date", embedding=_vec(0.40))
    assert matching_candidates("lease_end_date", _vec(1.0), [cand]) == []


def test_different_facet_moderate_similarity_does_not_collide() -> None:
    # The Codex #2 fix: distinct-but-similar facts (0.85) do not supersede.
    cand = _cand(facet="monthly_rent", embedding=_vec(0.85))
    assert matching_candidates("lease_end_date", _vec(1.0), [cand]) == []


def test_different_facet_near_identical_recovers_from_drift() -> None:
    # Drift: same fact, different LLM facet string, near-identical content (>= 0.95) merges.
    cand = _cand(facet="current_lease_expiration", embedding=_vec(0.97))
    matched = matching_candidates("lease_end_date", _vec(1.0), [cand])
    assert matched == [cand]


def test_empty_facet_uses_base_threshold() -> None:
    # Legacy/no-facet claim: base 0.83 threshold (FRE-638 behavior).
    near = _cand(facet="", embedding=_vec(0.85), claim_id="near")
    far = _cand(facet="", embedding=_vec(0.70), claim_id="far")
    matched = matching_candidates("lease_end_date", _vec(1.0), [near, far])
    assert [c.claim_id for c in matched] == ["near"]


def test_new_facet_matches_legacy_empty_facet_claim() -> None:
    # New claim has a facet, old row predates facets (empty) → neutral base threshold.
    legacy = _cand(facet="", embedding=_vec(0.90))
    assert matching_candidates("lease_end_date", _vec(1.0), [legacy]) == [legacy]


# --- strongest_blocker: adjudicate against the strongest safety blocker ---------


def test_strongest_blocker_picks_highest_confidence() -> None:
    weak = _cand(confidence=0.6, claim_id="weak")
    strong = _cand(confidence=0.9, claim_id="strong")
    assert strongest_blocker([weak, strong]).claim_id == "strong"


def test_strongest_blocker_breaks_ties_by_freshness() -> None:
    old = _cand(confidence=0.8, observed_at=_T0, claim_id="old")
    new = _cand(confidence=0.8, observed_at=_T1, claim_id="new")
    assert strongest_blocker([old, new]).claim_id == "new"


def test_strongest_blocker_empty_is_none() -> None:
    assert strongest_blocker([]) is None


def test_thresholds_ordered_and_conservative() -> None:
    assert SAME_FACET_FLOOR < CLAIM_MATCH_THRESHOLD < DIFF_FACET_FLOOR
    assert 0.5 <= SAME_FACET_FLOOR and DIFF_FACET_FLOOR <= 0.97
