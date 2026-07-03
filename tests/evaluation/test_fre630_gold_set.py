"""FRE-630 — gold-set discipline validation (pure).

Encodes the acceptance for the committed extraction-quality gold set: it must load
through the GoldCase schema, hit the agreed size (N≈24), be free of PII (public repo),
and represent the ticket's named failure modes so a passing benchmark actually
exercises the residence-vs-visit, hallucination, dedup, and stance/claim signals.
"""

from __future__ import annotations

from pathlib import Path

from scripts.eval.fre630_extraction_quality.gold import (
    ALLOWED_ENTITY_CLASSES,
    ALLOWED_ENTITY_TYPES,
    ALLOWED_REL_TYPES,
    all_authored_strings,
    load_gold_set,
)

GOLD_PATH = Path("scripts/eval/fre630_extraction_quality/gold_extraction.yaml")

#: N intended for the seed/regression set (owner-approved Phase-1 scope).
MIN_CASES = 20

#: Tokens that would indicate leaked private content in a public repo. Matched
#: case-insensitively against every authored string (mirrors the FRE-489 denylist).
PII_DENYLIST = {
    "alex",
    "kookier",
    "icloud.com",
    "@",
    "cf-access",
    "starry-plaza",
}


def _load() -> list:
    return load_gold_set(GOLD_PATH)


def test_gold_set_loads_and_hits_size() -> None:
    """The gold set loads and meets the minimum case count."""
    cases = _load()
    assert len(cases) >= MIN_CASES


def test_case_ids_unique() -> None:
    """Case ids are unique across the set (the loader also enforces this)."""
    cases = _load()
    ids = [c.case_id for c in cases]
    assert len(set(ids)) == len(ids)


def test_every_case_has_positive_label() -> None:
    """Every case carries at least one positive expectation (no degenerate cases)."""
    for case in _load():
        assert case.has_positive_label, f"{case.case_id} has no positive expectation"


def test_vocabulary_is_respected() -> None:
    """All entity types/classes and relationship types are in the controlled vocab."""
    for case in _load():
        for e in case.expect_entities:
            assert e.entity_type in ALLOWED_ENTITY_TYPES, f"{case.case_id}: {e.entity_type}"
            assert e.knowledge_class in ALLOWED_ENTITY_CLASSES, (
                f"{case.case_id}: {e.knowledge_class}"
            )
        for r in case.expect_relationships:
            assert r.rel_type in ALLOWED_REL_TYPES, f"{case.case_id}: {r.rel_type}"


def test_relationship_endpoints_reference_gold_entities() -> None:
    """Every gold edge's endpoints are themselves gold entities (else it can never score)."""
    for case in _load():
        names = {e.name for e in case.expect_entities}
        for r in case.expect_relationships:
            assert r.source in names, f"{case.case_id}: edge source {r.source!r} not a gold entity"
            assert r.target in names, f"{case.case_id}: edge target {r.target!r} not a gold entity"


def test_stance_and_claim_targets_are_sane() -> None:
    """Stance targets are extracted entities; claim gists are non-empty."""
    for case in _load():
        entity_names = {e.name for e in case.expect_entities}
        for s in case.expect_stances:
            assert s.target, f"{case.case_id}: empty stance target"
            assert s.target in entity_names, (
                f"{case.case_id}: stance target {s.target!r} not extracted"
            )
        for c in case.expect_claims:
            assert c.content_gist, f"{case.case_id}: empty claim gist"


def test_no_pii_tokens() -> None:
    """No PII/injected-email tokens leak into the public gold set."""
    offenders: list[str] = []
    for s in all_authored_strings(_load()):
        low = s.lower()
        for token in PII_DENYLIST:
            if token in low:
                offenders.append(f"{token!r} in {s!r}")
    assert not offenders, "PII tokens found:\n" + "\n".join(offenders)


def test_failure_modes_are_represented() -> None:
    """The ticket's named failure modes and all three knowledge classes are covered."""
    cases = _load()
    tags = {t for c in cases for t in c.tags}
    assert "residence-vs-visit" in tags
    assert "hallucination" in tags
    assert "dedup" in tags
    assert "stance" in tags or "claim" in tags

    # Structural: at least one of each trap family actually carries a trap.
    assert any(c.forbid_rel_types for c in cases), "no residence-vs-visit rel-type trap"
    assert any(c.forbid_entities for c in cases), "no hallucination entity trap"
    assert any(c.dedup_variants for c in cases), "no dedup-variant pair"
    assert any(c.expect_stances for c in cases), "no stance-emission case"
    assert any(c.expect_claims for c in cases), "no claim-emission case"
    # All three knowledge classes are exercised.
    classes = {e.knowledge_class for c in cases for e in c.expect_entities}
    assert classes == ALLOWED_ENTITY_CLASSES, f"classes covered: {classes}"
