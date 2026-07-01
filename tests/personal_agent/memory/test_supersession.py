"""Unit tests for the pure Claim supersession adjudication (FRE-638 / ADR-0098 D2).

These test the correction-vs-evolution decision and the embedding-similarity
candidate selection in isolation — no Neo4j, no embedder — so the living-knowledge
invariants (AC-1 correction, AC-2 bitemporal evolution) rest on deterministic logic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from personal_agent.memory.supersession import (
    CLAIM_MATCH_THRESHOLD,
    ClaimRecord,
    SupersessionAction,
    adjudicate,
    best_candidate,
)

_T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(days=90)


def _cand(*, confidence: float, observed_at: datetime, embedding: list[float]) -> ClaimRecord:
    return ClaimRecord(
        claim_id="cand-1",
        content="candidate",
        confidence=confidence,
        observed_at=observed_at,
        embedding=embedding,
    )


# --- adjudicate ------------------------------------------------------------


def test_no_candidate_is_fresh() -> None:
    result = adjudicate(new_confidence=0.8, new_observed_at=_T1, candidate=None)
    assert result.action is SupersessionAction.FRESH
    assert result.reason is None


def test_equal_confidence_newer_time_is_evolution() -> None:
    """AC-2: the fact was true and changed — bitemporal supersession, reason evolution."""
    cand = _cand(confidence=0.8, observed_at=_T0, embedding=[1.0, 0.0])
    result = adjudicate(new_confidence=0.8, new_observed_at=_T1, candidate=cand)
    assert result.action is SupersessionAction.SUPERSEDE
    assert result.reason == "evolution"


def test_higher_confidence_is_correction() -> None:
    """AC-1: the stored fact was wrong — higher-confidence re-assertion corrects it."""
    cand = _cand(confidence=0.5, observed_at=_T0, embedding=[1.0, 0.0])
    result = adjudicate(new_confidence=0.8, new_observed_at=_T1, candidate=cand)
    assert result.action is SupersessionAction.SUPERSEDE
    assert result.reason == "correction"


def test_higher_confidence_equal_time_is_correction() -> None:
    cand = _cand(confidence=0.5, observed_at=_T0, embedding=[1.0, 0.0])
    result = adjudicate(new_confidence=0.8, new_observed_at=_T0, candidate=cand)
    assert result.action is SupersessionAction.SUPERSEDE
    assert result.reason == "correction"


def test_lower_confidence_is_rejected() -> None:
    """ADR-0098 D2: not naive last-write-wins — a weaker later claim must not clobber."""
    cand = _cand(confidence=0.8, observed_at=_T0, embedding=[1.0, 0.0])
    result = adjudicate(new_confidence=0.5, new_observed_at=_T1, candidate=cand)
    assert result.action is SupersessionAction.REJECT


def test_stale_out_of_order_claim_is_rejected() -> None:
    """An older observation arriving after a newer current claim must not clobber it."""
    cand = _cand(confidence=0.8, observed_at=_T1, embedding=[1.0, 0.0])
    result = adjudicate(new_confidence=0.9, new_observed_at=_T0, candidate=cand)
    assert result.action is SupersessionAction.REJECT


# --- best_candidate --------------------------------------------------------


def test_best_candidate_picks_highest_similarity_above_threshold() -> None:
    near = _cand(confidence=0.8, observed_at=_T0, embedding=[1.0, 0.0])
    near = ClaimRecord(**{**near.__dict__, "claim_id": "near"})
    far = ClaimRecord(
        claim_id="far", content="c", confidence=0.8, observed_at=_T0, embedding=[0.0, 1.0]
    )
    match = best_candidate([0.99, 0.01], [far, near])
    assert match is not None
    assert match.claim_id == "near"


def test_best_candidate_none_when_all_below_threshold() -> None:
    orthogonal = ClaimRecord(
        claim_id="o", content="c", confidence=0.8, observed_at=_T0, embedding=[0.0, 1.0]
    )
    assert best_candidate([1.0, 0.0], [orthogonal]) is None


def test_best_candidate_empty_is_none() -> None:
    assert best_candidate([1.0, 0.0], []) is None


def test_threshold_is_conservative() -> None:
    # Guards against unrelated facts colliding (Codex #2); documented tunable.
    assert 0.7 <= CLAIM_MATCH_THRESHOLD <= 0.95
