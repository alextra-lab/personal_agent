"""FRE-817 -- unit tests for the ADR-0112 AC-4 pre-registered margin decision."""

from __future__ import annotations

import pytest
from scripts.eval.fre817_corpus_ab_embedder.decision import (
    PRE_REGISTERED_MARGIN_NDCG,
    EmbedderCandidate,
    decide_embedder,
)


def test_best_open_weight_wins_with_no_closed_candidate() -> None:
    """No closed candidate in the run -- the higher-nDCG open-weight arm wins outright."""
    candidates = [
        EmbedderCandidate(name="0.6b", kind="open_weight", mean_ndcg=0.60),
        EmbedderCandidate(name="8b-ovh", kind="open_weight", mean_ndcg=0.75),
    ]
    decision = decide_embedder(candidates, margin=PRE_REGISTERED_MARGIN_NDCG)
    assert decision.winner == "8b-ovh"
    assert decision.winner_kind == "open_weight"
    assert decision.margin_cleared is None


def test_closed_candidate_wins_when_it_clears_the_margin() -> None:
    """A closed candidate that beats the open-weight best by >= margin wins."""
    candidates = [
        EmbedderCandidate(name="8b-ovh", kind="open_weight", mean_ndcg=0.70),
        EmbedderCandidate(name="closed-x", kind="closed", mean_ndcg=0.80),
    ]
    decision = decide_embedder(candidates, margin=0.05)
    assert decision.winner == "closed-x"
    assert decision.winner_kind == "closed"
    assert decision.margin_cleared is True


def test_closed_candidate_loses_when_it_falls_short_of_the_margin() -> None:
    """A closed candidate below the margin is a noise-level win -- open-weight is retained."""
    candidates = [
        EmbedderCandidate(name="8b-ovh", kind="open_weight", mean_ndcg=0.70),
        EmbedderCandidate(name="closed-x", kind="closed", mean_ndcg=0.72),
    ]
    decision = decide_embedder(candidates, margin=0.05)
    assert decision.winner == "8b-ovh"
    assert decision.winner_kind == "open_weight"
    assert decision.margin_cleared is False


def test_exact_margin_boundary_clears() -> None:
    """`delta == margin` clears (`>=`, not `>`)."""
    candidates = [
        EmbedderCandidate(name="8b-ovh", kind="open_weight", mean_ndcg=0.70),
        EmbedderCandidate(name="closed-x", kind="closed", mean_ndcg=0.75),
    ]
    decision = decide_embedder(candidates, margin=0.05)
    assert decision.winner == "closed-x"
    assert decision.margin_cleared is True


def test_empty_candidates_raises() -> None:
    """No candidates means no A/B artifact -- must raise, never decide vacuously."""
    with pytest.raises(ValueError, match="no embedder candidates"):
        decide_embedder([], margin=PRE_REGISTERED_MARGIN_NDCG)


def test_no_open_weight_candidate_raises() -> None:
    """AC-4's retained default requires at least one open-weight candidate."""
    candidates = [EmbedderCandidate(name="closed-x", kind="closed", mean_ndcg=0.90)]
    with pytest.raises(ValueError, match="open-weight"):
        decide_embedder(candidates, margin=PRE_REGISTERED_MARGIN_NDCG)


@pytest.mark.parametrize("bad_margin", [0.0, -0.01])
def test_nonpositive_margin_raises(bad_margin: float) -> None:
    """A zero/negative margin would let a closed model win on a tie or a loss -- refuse it."""
    candidates = [
        EmbedderCandidate(name="8b-ovh", kind="open_weight", mean_ndcg=0.70),
        EmbedderCandidate(name="closed-x", kind="closed", mean_ndcg=0.99),
    ]
    with pytest.raises(ValueError, match="margin"):
        decide_embedder(candidates, margin=bad_margin)


def test_multiple_open_weight_and_multiple_closed_picks_best_of_each() -> None:
    """With multiple candidates per kind, only the best-of-each pair matters to the decision."""
    candidates = [
        EmbedderCandidate(name="0.6b", kind="open_weight", mean_ndcg=0.55),
        EmbedderCandidate(name="8b-ovh", kind="open_weight", mean_ndcg=0.70),
        EmbedderCandidate(name="closed-weak", kind="closed", mean_ndcg=0.71),
        EmbedderCandidate(name="closed-strong", kind="closed", mean_ndcg=0.90),
    ]
    decision = decide_embedder(candidates, margin=0.05)
    assert decision.winner == "closed-strong"
    assert decision.margin_cleared is True
