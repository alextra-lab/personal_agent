"""Unit tests for the closed-axis structural arm Cypher builder (FRE-707, ADR-0104 AC-4).

These are pure tests over ``_build_structural_arm_query`` — no Neo4j. They prove the
falsifiable shape guarantees of the structural arm:

* AC-4a — the type predicate is absent when the type sub-predicate is disabled.
* AC-4b — when enabled, the type predicate keeps unenforced (``""``/``NULL``/``Unknown``) rows.
* AC-4c — no clause ever filters on the open axis (name/description).
* FRE-229 — the co-occurrence traversal scopes the intermediate Turn and the anchor.
* Recency is compared with ``toString`` (last_seen is heterogeneous in the substrate).
"""

from __future__ import annotations

from personal_agent.memory.service import _build_structural_arm_query

# Sentinel visibility fragments so the test can assert per-alias scoping without
# depending on the exact chokepoint fragment text.
_VIS_E = "(e.visibility IS NULL OR e.visibility = 'public')"
_VIS_T = "(t.visibility IS NULL OR t.visibility = 'public')"
_VIS_A = "(a.visibility IS NULL OR a.visibility = 'public')"


def _build(**overrides: object) -> tuple[str, dict[str, object]]:
    """Call the builder with sane defaults, overriding named kwargs."""
    kwargs: dict[str, object] = {
        "entity_types": None,
        "type_predicate_enabled": False,
        "recency_days": None,
        "anchor_names": None,
        "entity_classes": None,
        "class_predicate_enabled": False,
        "top_k": 50,
        "vis_fragment_e": _VIS_E,
        "vis_fragment_t": _VIS_T,
        "vis_fragment_a": _VIS_A,
    }
    kwargs.update(overrides)
    return _build_structural_arm_query(**kwargs)  # type: ignore[arg-type]


def test_class_predicate_keeps_unclassified_rows() -> None:
    """ADR-0115 D6 / FRE-866: an enabled class predicate keeps NULL-class rows."""
    cypher, params = _build(entity_classes=["World"], class_predicate_enabled=True)
    assert "e.class IN $entity_classes" in cypher
    assert "e.class IS NULL" in cypher
    assert params["entity_classes"] == ["World"]


def test_class_predicate_absent_when_disabled() -> None:
    """No class predicate when the sub-predicate is gated off."""
    cypher, params = _build(entity_classes=["World"], class_predicate_enabled=False)
    assert "e.class" not in cypher
    assert "entity_classes" not in params


def test_class_predicate_absent_without_classes() -> None:
    """Enabled flag but entity_classes=None → no class predicate."""
    cypher, params = _build(entity_classes=None, class_predicate_enabled=True)
    assert "e.class" not in cypher
    assert "entity_classes" not in params


def test_class_predicate_absent_with_empty_list() -> None:
    """Enabled flag but entity_classes=[] → no class predicate (falsy guard)."""
    cypher, params = _build(entity_classes=[], class_predicate_enabled=True)
    assert "e.class" not in cypher
    assert "entity_classes" not in params


def test_item_id_present_in_both_branches() -> None:
    """FRE-866: elementId(e) is projected in the plain-scan and anchor-traversal Cypher."""
    plain_cypher, _ = _build()
    anchor_cypher, _ = _build(anchor_names=["Rafale"])
    assert "elementId(e) AS item_id" in plain_cypher
    assert "elementId(e) AS item_id" in anchor_cypher


def test_type_predicate_keeps_unenforced_rows() -> None:
    """AC-4b: an enabled type predicate keeps NULL/empty/Unknown rows."""
    cypher, params = _build(entity_types=["Person"], type_predicate_enabled=True)
    assert "e.entity_type IN $entity_types" in cypher
    # The escape hatches that prevent a silent drop of unenforced-type rows.
    assert "e.entity_type IS NULL" in cypher
    assert "e.entity_type = ''" in cypher
    assert "e.entity_type = 'Unknown'" in cypher
    assert params["entity_types"] == ["Person"]


def test_type_predicate_absent_when_disabled() -> None:
    """AC-4a: no type predicate when the sub-predicate is gated off."""
    cypher, params = _build(entity_types=["Person"], type_predicate_enabled=False)
    assert "entity_type" not in cypher
    assert "entity_types" not in params


def test_type_predicate_absent_without_types() -> None:
    """Enabled flag but no requested types → no type predicate."""
    cypher, params = _build(entity_types=None, type_predicate_enabled=True)
    assert "entity_type" not in cypher
    assert "entity_types" not in params


def test_no_open_axis_predicate() -> None:
    """AC-4c: no param combination filters on the open axis (name/description)."""
    for combo in (
        {},
        {"entity_types": ["Person"], "type_predicate_enabled": True},
        {"recency_days": 30},
        {"anchor_names": ["Rafale"]},
        {
            "entity_types": ["Person"],
            "type_predicate_enabled": True,
            "recency_days": 30,
            "anchor_names": ["Rafale"],
        },
    ):
        cypher, params = _build(**combo)
        # No hard filter on the free-text open axis.
        assert "e.name IN" not in cypher
        assert "e.name =" not in cypher
        assert "e.description" not in cypher
        assert "CONTAINS" not in cypher
        assert not any(
            "name" in k or "description" in k for k in params if k not in {"anchor_names"}
        )


def test_recency_predicate_uses_tostring() -> None:
    """Recency compares normalised strings (last_seen is heterogeneous)."""
    cypher, params = _build(recency_days=30)
    assert "toString(e.last_seen) >= $recency_cutoff" in cypher
    assert "recency_cutoff" in params


def test_no_recency_predicate_when_none() -> None:
    """No recency window → no last_seen predicate."""
    cypher, params = _build(recency_days=None)
    assert "recency_cutoff" not in params
    assert ">= $recency_cutoff" not in cypher


def test_anchor_traversal_scopes_turn_and_anchor() -> None:
    """FRE-229: the co-occurrence traversal scopes a, t AND e."""
    cypher, params = _build(anchor_names=["Rafale"])
    assert "-[:DISCUSSES]->" in cypher
    assert "(t:Turn)" in cypher
    # All three matched nodes carry a visibility fragment.
    assert _VIS_A in cypher
    assert _VIS_T in cypher
    assert _VIS_E in cypher
    assert params["anchor_names"] == ["Rafale"]
    # Ordering normalises last_seen for the mixed string/temporal representation.
    assert "toString(e.last_seen) DESC" in cypher


def test_plain_scan_has_no_turn_match() -> None:
    """Without anchors the arm is a plain entity scan (no Turn hop)."""
    cypher, _ = _build()
    assert "(t:Turn)" not in cypher
    assert "MATCH (e:Entity)" in cypher
    assert _VIS_E in cypher


def test_top_k_param_passed() -> None:
    """The retrieval depth is bound by top_k."""
    _, params = _build(top_k=17)
    assert params["top_k"] == 17
