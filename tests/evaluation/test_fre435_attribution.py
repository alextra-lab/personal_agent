"""FRE-488 — hypothesis-attribution tests (pure).

One synthetic :class:`AttributionInput` per D4 hypothesis bucket, plus the
gate-ordering edge (not-retrievable beats denial).
"""

from __future__ import annotations

from scripts.eval.fre435_memory_recall.attribution import (
    AttributionInput,
    Hypothesis,
    attribute,
)


def _inp(**kw: object) -> AttributionInput:
    base: dict[str, object] = {
        "failed": True,
        "expected_writes": 1,
        "entities_landed": 1,
        "description_integrity": None,
        "false_negative": False,
        "recall_at_prod_k": 1.0,
        "recall_at_max_k": 1.0,
    }
    base.update(kw)
    return AttributionInput(**base)  # type: ignore[arg-type]


def test_pass_when_not_failed() -> None:
    """Pass when not failed."""
    assert attribute(_inp(failed=False)) is Hypothesis.PASS


def test_h1_write_gap_when_nothing_landed() -> None:
    """H1 write gap when nothing landed."""
    assert attribute(_inp(expected_writes=2, entities_landed=0)) is Hypothesis.H1_WRITE_GAP


def test_h2_frozen_description_below_bar() -> None:
    """H2 frozen description below bar."""
    assert attribute(_inp(description_integrity=0.2)) is Hypothesis.H2_FROZEN_DESCRIPTION


def test_h3_not_retrievable_at_any_k() -> None:
    """H3 not retrievable at any k."""
    # Landed but never surfaces even at the widest k -> retrieval path.
    assert (
        attribute(_inp(recall_at_max_k=0.0, recall_at_prod_k=0.0)) is Hypothesis.H3_RETRIEVAL_RANK
    )


def test_h3_ranked_too_low() -> None:
    """H3 ranked too low."""
    # In the index at large k, missed at production k -> ranked too low.
    assert (
        attribute(_inp(recall_at_max_k=1.0, recall_at_prod_k=0.0)) is Hypothesis.H3_RETRIEVAL_RANK
    )


def test_h4_threshold_false_negative() -> None:
    """H4 threshold false negative."""
    # Present AND retrievable, but the system denied -> threshold / query construction.
    assert (
        attribute(_inp(false_negative=True, recall_at_max_k=1.0, recall_at_prod_k=1.0))
        is Hypothesis.H4_THRESHOLD_FN
    )


def test_not_retrievable_beats_denial_in_ordering() -> None:
    """Not retrievable beats denial in ordering."""
    # Denied AND not retrievable at any k: the retrieval failure is the root cause.
    assert (
        attribute(_inp(false_negative=True, recall_at_max_k=0.0, recall_at_prod_k=0.0))
        is Hypothesis.H3_RETRIEVAL_RANK
    )


def test_h5_h6_residual_when_present_retrievable_but_failed() -> None:
    """H5 h6 residual when present retrievable but failed."""
    # Failed, fact landed + retrievable at prod k, no write/desc/rank/denial cause.
    assert (
        attribute(_inp(failed=True, recall_at_max_k=1.0, recall_at_prod_k=1.0))
        is Hypothesis.H5_H6_ARCHITECTURE
    )
