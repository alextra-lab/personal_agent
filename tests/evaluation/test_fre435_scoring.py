"""FRE-488 — pure scoring-seam tests (no substrate, no LLM).

Covers the flatten/namespace/dedup of a recall result and the per-case scoring
that turns a retrieval outcome into a :class:`CaseResult`.
"""

from __future__ import annotations

from scripts.eval.fre435_memory_recall.attribution import Hypothesis
from scripts.eval.fre435_memory_recall.metrics import WriteOutcome
from scripts.eval.fre435_memory_recall.probes import ExpectedRecall, ProbeCase, SeedEntity
from scripts.eval.fre435_memory_recall.scoring import flatten_recall, score_case

# ---------------------------------------------------------------------------
# flatten_recall — namespacing + ordering + dedup (codex Q3)
# ---------------------------------------------------------------------------


def test_flatten_namespaces_entities_and_episodes() -> None:
    """Flatten namespaces entities and episodes."""
    ids = flatten_recall(
        episodes=[{"turn_id": "t1"}],
        entities=[{"name": "Diffraction Limit"}],
        relevance_scores={},
    )
    assert "entity:diffraction limit" in ids
    assert "episode:t1" in ids


def test_flatten_orders_by_relevance_desc() -> None:
    """Flatten orders by relevance desc."""
    ids = flatten_recall(
        episodes=[],
        entities=[{"name": "Low"}, {"name": "High"}],
        relevance_scores={"Low": 0.1, "High": 0.9},
    )
    assert ids == ("entity:high", "entity:low")


def test_flatten_dedups_keeping_first() -> None:
    """Flatten dedups keeping first."""
    ids = flatten_recall(
        episodes=[{"turn_id": "t1"}],
        entities=[{"name": "A"}, {"name": "a"}],  # same namespaced id
        relevance_scores={},
    )
    assert ids.count("entity:a") == 1


def test_flatten_cross_namespace_no_collision() -> None:
    """Flatten cross namespace no collision."""
    # An entity named "t1" and an episode turn_id "t1" must NOT collide.
    ids = flatten_recall(
        episodes=[{"turn_id": "t1"}],
        entities=[{"name": "t1"}],
        relevance_scores={},
    )
    assert set(ids) == {"episode:t1", "entity:t1"}


# ---------------------------------------------------------------------------
# score_case
# ---------------------------------------------------------------------------


def _case() -> ProbeCase:
    return ProbeCase(
        case_id="c1",
        query="q",
        seed_entities=(SeedEntity(name="Diffraction Limit"),),
        expected=ExpectedRecall(entity_names=("Diffraction Limit",), must_not_deny=True),
        tags=("positive",),
    )


def test_score_case_hit_is_not_failed_and_passes() -> None:
    """Score case hit is not failed and passes."""
    res = score_case(
        case=_case(),
        retrieved=("entity:diffraction limit",),
        denied=False,
        write_outcome=WriteOutcome(extraction_fired=True, entities_landed=1, entities_expected=1),
        prod_k=3,
        k_sweep=(1, 3),
    )
    assert res.recall_by_k[3] == 1.0
    assert res.false_negative is False
    assert res.failed is False
    assert res.hypothesis is Hypothesis.PASS


def test_score_case_denied_is_false_negative_h4() -> None:
    """Score case denied is false negative h4."""
    res = score_case(
        case=_case(),
        retrieved=(),
        denied=True,
        write_outcome=WriteOutcome(extraction_fired=True, entities_landed=1, entities_expected=1),
        prod_k=3,
        k_sweep=(1, 3),
    )
    assert res.false_negative is True
    assert res.failed is True
    # retrieved empty -> not retrievable at any k -> H3 takes root precedence over denial
    assert res.hypothesis is Hypothesis.H3_RETRIEVAL_RANK


def test_score_case_write_gap_h1() -> None:
    """Score case write gap h1."""
    res = score_case(
        case=_case(),
        retrieved=(),
        denied=False,
        write_outcome=WriteOutcome(extraction_fired=False, entities_landed=0, entities_expected=1),
        prod_k=3,
        k_sweep=(1, 3),
    )
    assert res.failed is True
    assert res.hypothesis is Hypothesis.H1_WRITE_GAP


def test_score_case_control_no_relevant_is_pass() -> None:
    """Score case control no relevant is pass."""
    control = ProbeCase(
        case_id="ctrl",
        query="q",
        expected=ExpectedRecall(entity_names=(), must_not_deny=False),
    )
    res = score_case(
        case=control,
        retrieved=(),
        denied=True,
        write_outcome=WriteOutcome(extraction_fired=True, entities_landed=0, entities_expected=0),
        prod_k=3,
        k_sweep=(1, 3),
    )
    assert res.relevant_count == 0
    assert res.false_negative is None
    assert res.failed is False
    assert res.hypothesis is Hypothesis.PASS
