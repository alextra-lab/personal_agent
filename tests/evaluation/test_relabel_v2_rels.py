"""FRE-773 — unit tests for the blind relationship-relabel driver.

Pure/plumbing tests only (no real API calls): tolerant response parsing, item
collection over gold triples, the dry-run stub shape, prompt-hash stability, and
the NONE-as-its-own-category IAA wiring. The real 3-rater run is exercised
manually via ``--dry-run`` and the live run, not in the unit suite.
"""

from __future__ import annotations

from scripts.eval.fre630_extraction_quality.gold import (
    ALLOWED_REL_TYPES_V2,
    REL_V2_NO_EDGE,
    GoldCase,
    GoldEntity,
    GoldRelationship,
)
from scripts.eval.fre630_extraction_quality.relabel_v2_rels import (
    RATERS,
    REL_CLASSIFY_CATEGORIES,
    _dry_run_response,
    _parse_rater_response,
    build_report,
    collect_rel_items,
    prompt_hash,
)


def _case_with_rels(case_id: str, rels: tuple[GoldRelationship, ...]) -> GoldCase:
    """A minimal loadable gold case carrying the given relationships."""
    names = {name for r in rels for name in (r.source, r.target)}
    entities = tuple(
        GoldEntity(name=n, entity_type="Concept", knowledge_class="World") for n in sorted(names)
    )
    return GoldCase(
        case_id=case_id,
        tags=("t",),
        source_user="u",
        source_assistant="a",
        expect_entities=entities,
        expect_relationships=rels,
    )


def test_parse_valid_type() -> None:
    """A well-formed JSON response with an in-vocab type parses cleanly."""
    resp = _parse_rater_response('{"type": "USES", "rationale": "A depends on B"}')
    assert resp.rel_label == "USES"
    assert resp.rationale == "A depends on B"
    assert resp.error is None


def test_parse_none_outcome() -> None:
    """The NONE emit-nothing outcome is a first-class label, not an error."""
    resp = _parse_rater_response('{"type": "NONE", "rationale": "no edge holds"}')
    assert resp.rel_label == REL_V2_NO_EDGE
    assert resp.error is None


def test_parse_off_vocab_type_is_a_data_point() -> None:
    """An off-vocabulary type is recorded as an error, not raised."""
    resp = _parse_rater_response('{"type": "MENTIONS", "rationale": "x"}')
    assert resp.rel_label == ""
    assert resp.error == "off_vocab_type"


def test_parse_no_json() -> None:
    """A response with no JSON object is a tolerant parse failure."""
    resp = _parse_rater_response("I think it is USES.")
    assert resp.rel_label == ""
    assert resp.error == "no_json_found"


def test_collect_rel_items_flattens_every_triple() -> None:
    """Every gold relationship becomes exactly one classification item."""
    cases = [
        _case_with_rels(
            "c1",
            (
                GoldRelationship(source="A", rel_type="USES", target="B"),
                GoldRelationship(source="B", rel_type="PART_OF", target="C"),
            ),
        ),
        _case_with_rels("c2", (GoldRelationship(source="X", rel_type="RELATED_TO", target="Y"),)),
    ]
    items = collect_rel_items(cases)
    assert [i.item_id for i in items] == ["c1::A->B", "c1::B->C", "c2::X->Y"]
    assert items[0].source == "A" and items[0].target == "B"


def test_dry_run_response_is_schema_valid() -> None:
    """The dry-run stub yields an in-vocab label for every rater."""
    for rater in RATERS:
        resp = _dry_run_response(rater)
        assert resp.rel_label in ALLOWED_REL_TYPES_V2
        assert resp.error is None


def test_prompt_hash_is_stable() -> None:
    """The prompt hash is deterministic across calls (pins the definition revision)."""
    assert prompt_hash() == prompt_hash()
    assert len(prompt_hash()) == 12


def test_none_is_a_distinct_iaa_category() -> None:
    """NONE participates in the IAA category set alongside the 6 real types."""
    assert REL_V2_NO_EDGE in REL_CLASSIFY_CATEGORIES
    assert set(ALLOWED_REL_TYPES_V2).issubset(set(REL_CLASSIFY_CATEGORIES))


def test_build_report_scores_only_complete_items() -> None:
    """build_report drops items where any rater failed, and reports over the rest.

    A type-vs-NONE split registers as a genuine disagreement (no silent coercion).
    """
    items = collect_rel_items(
        [
            _case_with_rels("c1", (GoldRelationship(source="A", rel_type="USES", target="B"),)),
            _case_with_rels("c2", (GoldRelationship(source="X", rel_type="USES", target="Y"),)),
        ]
    )
    rater_names = [r.name for r in RATERS]
    # c1: all three say USES (agreement). c2: two USES, one NONE (disagreement).
    from scripts.eval.fre630_extraction_quality.relabel_v2_rels import RaterResponse

    def _resp(label: str) -> RaterResponse:
        return RaterResponse(rel_label=label, rationale="", raw_text="{}")

    by_item = {
        "c1::A->B": {name: _resp("USES") for name in rater_names},
        "c2::X->Y": {
            rater_names[0]: _resp("USES"),
            rater_names[1]: _resp("USES"),
            rater_names[2]: _resp(REL_V2_NO_EDGE),
        },
    }
    report = build_report(items, by_item)
    assert report.overall.n_items == 2
    assert "c2::X->Y" in report.disagreements
    assert "c1::A->B" not in report.disagreements
