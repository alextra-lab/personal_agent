"""FRE-630 — gold-set discipline validation (pure).

Encodes the acceptance for the committed extraction-quality gold set: it must load
through the GoldCase schema, hit the agreed size (N≈24), be free of PII (public repo),
and represent the ticket's named failure modes so a passing benchmark actually
exercises the residence-vs-visit, hallucination, dedup, and stance/claim signals.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from scripts.eval.fre630_extraction_quality.gold import (
    ALLOWED_ENTITY_CLASSES,
    ALLOWED_ENTITY_TYPES,
    ALLOWED_ENTITY_TYPES_V2,
    ALLOWED_REL_TYPES,
    ALLOWED_REL_TYPES_V2,
    REL_V2_NO_EDGE,
    all_authored_strings,
    load_gold_set,
)

GOLD_PATH = Path("scripts/eval/fre630_extraction_quality/gold_extraction.yaml")
BOUNDARY_FIXTURE_PATH = Path("scripts/eval/fre630_extraction_quality/fre782_boundary_fixture.yaml")

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


#: FRE-759 (codex P1.1) — distinct claim cases needed for a powered claim_emission_recall.
MIN_KEYED_CLAIM_CASES = 10


def test_claim_coverage_is_powered() -> None:
    """FRE-759: ≥10 distinct claim-bearing cases carry a keyed (non-empty facet) claim.

    ``claim_emission_recall`` excludes facetless claims from its denominator, so the
    measurable power is the count of DISTINCT cases with ≥1 keyed claim. ≥10 makes
    ``claim_emission_recall ≥0.8`` mean "≥8 of ≥10 distinct claim cases pass" rather
    than one or two sample flips on a 2-case set (the pre-FRE-759 hazard).
    """
    keyed = [c for c in _load() if any(cl.facet for cl in c.expect_claims)]
    assert len(keyed) >= MIN_KEYED_CLAIM_CASES, (
        f"only {len(keyed)} keyed-claim cases; need ≥{MIN_KEYED_CLAIM_CASES} for a powered AC-2"
    )


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


# ── FRE-770: ADR-0109 V2 gold relabel (v2_type dual-field) ──────────────────


def test_all_entities_have_v2_type() -> None:
    """Every gold entity carries a V2 (ADR-0109 8-type) label, not just V1.

    A single-author or partial re-label does not satisfy FRE-770 — the ticket
    requires every entity re-labeled via the blind multi-rater pipeline.
    """
    offenders = [
        f"{c.case_id}:{e.name}"
        for c in _load()
        for e in c.expect_entities
        if e.v2_type not in ALLOWED_ENTITY_TYPES_V2
    ]
    assert not offenders, f"entities missing/invalid v2_type: {offenders}"


#: ADR-0109 names 5 Phenomenon examples in its own spot-check; FRE-770 must grow
#: coverage beyond them, so the committed gold carries strictly more.
MIN_PHENOMENON_ENTITIES = 6


def test_phenomenon_coverage_grown_beyond_adr_spotcheck() -> None:
    """Phenomenon coverage exceeds the ADR's own 5 spot-checked examples."""
    phenomena = [e.name for c in _load() for e in c.expect_entities if e.v2_type == "Phenomenon"]
    assert len(phenomena) >= MIN_PHENOMENON_ENTITIES, (
        f"only {len(phenomena)} Phenomenon entities; need ≥{MIN_PHENOMENON_ENTITIES} "
        "to exceed the ADR's own 5-example spot-check"
    )


def test_no_unresolved_signoff_without_rationale() -> None:
    """Every entity flagged for owner sign-off carries a builder rationale.

    A 3-way rater split is still adjudicated (v2_type is never left empty) but
    flagged `v2_needs_owner_signoff` for later confirmation — that flag must
    never appear without the reasoning that produced it.
    """
    offenders = [
        f"{c.case_id}:{e.name}"
        for c in _load()
        for e in c.expect_entities
        if e.v2_needs_owner_signoff and not e.v2_adjudication_rationale
    ]
    assert not offenders, f"sign-off flagged with no rationale: {offenders}"


# ── FRE-773: ADR-0109 V2 relationship relabel (v2_rel_type dual-field) ───────


def test_all_relationships_have_v2_rel_type() -> None:
    """Every gold relationship carries a V2 (ADR-0109) label, not just V1.

    A valid ``v2_rel_type`` is one of the 6 V2 keys or the ``REL_V2_NO_EDGE``
    marker (the V2 vocab saying no edge should exist). FRE-773 requires every
    relationship re-labeled via the blind multi-rater pipeline — a partial or
    single-author relabel does not satisfy the ticket.
    """
    allowed = ALLOWED_REL_TYPES_V2 | {REL_V2_NO_EDGE}
    offenders = [
        f"{c.case_id}:{r.source}->{r.target}"
        for c in _load()
        for r in c.expect_relationships
        if r.v2_rel_type not in allowed
    ]
    assert not offenders, f"relationships missing/invalid v2_rel_type: {offenders}"


def test_rel_no_unresolved_signoff_without_rationale() -> None:
    """Every relationship flagged for owner sign-off carries a builder rationale.

    A 3-way split or a converged ``NONE`` is still adjudicated (``v2_rel_type``
    is never left empty) but flagged ``v2_needs_owner_signoff`` — that flag must
    never appear without the reasoning that produced it.
    """
    offenders = [
        f"{c.case_id}:{r.source}->{r.target}"
        for c in _load()
        for r in c.expect_relationships
        if r.v2_needs_owner_signoff and not r.v2_adjudication_rationale
    ]
    assert not offenders, f"rel sign-off flagged with no rationale: {offenders}"


def test_no_edge_marker_requires_signoff() -> None:
    """The ``REL_V2_NO_EDGE`` marker is never a silent, clean-looking label.

    Codex "no silent coercion" contract (FRE-773): a rater-converged "no edge"
    outcome must surface as an owner decision, so any relationship whose
    ``v2_rel_type`` is the NONE marker MUST also carry ``v2_needs_owner_signoff``
    and a rationale — never a bare marker that reads as settled.
    """
    offenders = [
        f"{c.case_id}:{r.source}->{r.target}"
        for c in _load()
        for r in c.expect_relationships
        if r.v2_rel_type == REL_V2_NO_EDGE
        and not (r.v2_needs_owner_signoff and r.v2_adjudication_rationale)
    ]
    assert not offenders, f"NONE marker without sign-off+rationale: {offenders}"


def test_similar_to_coverage() -> None:
    """At least one relationship is labeled SIMILAR_TO under V2.

    The V1 gold had zero SIMILAR_TO edges; FRE-773 grows coverage of the ADR's
    named relationship faults, and a vocab type with no example can't be
    measured for agreement at all.
    """
    similar = [
        f"{c.case_id}:{r.source}->{r.target}"
        for c in _load()
        for r in c.expect_relationships
        if r.v2_rel_type == "SIMILAR_TO"
    ]
    assert similar, "no relationship labeled SIMILAR_TO under V2"


# ── FRE-782/784: ADR-0109 Amendment 1 (8→10 types: KnowledgeArtifact + QuantityMeasure) ──


def _find_entity(case_id: str, name: str):
    """Look up one gold entity by (case_id, name), for a single re-typed assertion."""
    for case in _load():
        if case.case_id == case_id:
            for entity in case.expect_entities:
                if entity.name == name:
                    return entity
    raise AssertionError(f"entity {name!r} not found in case {case_id!r}")


def test_v2_entity_types_is_ten_types() -> None:
    """Amendment 1 grows the V2 entity vocabulary from 8 to 10 types.

    `KnowledgeArtifact` (human-authored works) and `QuantityMeasure` (physical
    quantities) were validated to the same entity bar as the original eight
    (FRE-782, 3-rater boundary IAA, overall kappa 0.900) and promoted here.
    """
    assert ALLOWED_ENTITY_TYPES_V2 == frozenset(
        {
            "Person",
            "Organization",
            "Location",
            "TechnicalArtifact",
            "KnowledgeArtifact",
            "MethodOrConcept",
            "DomainOrTopic",
            "Phenomenon",
            "QuantityMeasure",
            "Event",
        }
    )


def test_neuroplasticity_chapter_types_knowledge_artifact() -> None:
    """The Amendment 1 case resolution: `Neuroplasticity Chapter` -> KnowledgeArtifact.

    FRE-770 could only provisionally rule this TechnicalArtifact (a genuine 3-way
    rater split) and flagged it for owner sign-off. FRE-782's boundary probe
    resolved it unanimously (3/3) as KnowledgeArtifact, so the sign-off flag
    must now be cleared.
    """
    entity = _find_entity("personal-writing-project", "Neuroplasticity Chapter")
    assert entity.v2_type == "KnowledgeArtifact"
    assert entity.v2_needs_owner_signoff is False


def test_wavelength_types_quantity_measure() -> None:
    """The Amendment 1 case resolution: `Wavelength` -> QuantityMeasure.

    FRE-770 majority-ruled this MethodOrConcept while flagging the gap
    (physical quantities had no clean home in the 8-type vocabulary). FRE-782's
    boundary probe resolved it unanimously (3/3) as QuantityMeasure.
    """
    entity = _find_entity("physics-scattering", "Wavelength")
    assert entity.v2_type == "QuantityMeasure"


#: The FRE-782 research note's per-entity table (appendix), reproduced exactly as
#: ``{entity: (intended_side, boundary)}`` so the checked-in fixture is checked for
#: fidelity to the source, not just aggregate counts.
EXPECTED_BOUNDARY_PROBE: dict[str, tuple[str, bool]] = {
    "ADR-0109": ("KnowledgeArtifact", False),
    "GoLLIE paper": ("KnowledgeArtifact", False),
    "architecture redesign spec": ("KnowledgeArtifact", False),
    "Neuroplasticity Chapter": ("KnowledgeArtifact", False),
    "master plan": ("KnowledgeArtifact", False),
    "outage post-mortem report": ("KnowledgeArtifact", False),
    "FRE-630 gold set": ("TechnicalArtifact", False),
    "extraction prompt": ("TechnicalArtifact", False),
    "gold_extraction.yaml": ("TechnicalArtifact", False),
    "Neo4j": ("TechnicalArtifact", False),
    "GPU": ("TechnicalArtifact", False),
    "FastAPI": ("TechnicalArtifact", False),
    "Wavelength": ("QuantityMeasure", False),
    "Mass": ("QuantityMeasure", False),
    "Temperature": ("QuantityMeasure", False),
    "Frequency": ("QuantityMeasure", False),
    "Luminosity": ("QuantityMeasure", False),
    "Redshift": ("QuantityMeasure", True),
    "Gravity": ("Phenomenon", False),
    "Rayleigh Scattering": ("Phenomenon", False),
    "Diffraction Limit": ("Phenomenon", True),
    "Fourier Transform": ("MethodOrConcept", False),
}


def test_boundary_fixture_matches_research_note() -> None:
    """The FRE-782 22-entity boundary probe is checked in as a re-runnable regression.

    Reproduces the research note's per-entity table exactly (docs/research/
    2026-07-04-fre-782-knowledgeartifact-quantitymeasure-boundary-iaa.md,
    appendix) — entity-for-entity, not just aggregate counts, so a swapped
    entity or a flipped intended_side fails this assertion.
    """
    doc = yaml.safe_load(BOUNDARY_FIXTURE_PATH.read_text(encoding="utf-8"))
    probe = doc["probe"]
    assert len(probe) == len(EXPECTED_BOUNDARY_PROBE) == 22

    actual = {item["entity"]: (item["intended_side"], bool(item.get("boundary"))) for item in probe}
    assert actual == EXPECTED_BOUNDARY_PROBE

    for intended_side, _ in actual.values():
        assert intended_side in ALLOWED_ENTITY_TYPES_V2


def test_v2_type_definitions_match_allowed_types() -> None:
    """The relabel script's GoLLIE definitions cover exactly the 10 allowed types.

    A stale 8-type `V2_TYPE_DEFINITIONS` (missing `KnowledgeArtifact` /
    `QuantityMeasure`) would let a rater never see the new types at all —
    AC-8's "stale eight-type prompt" failure mode.
    """
    from scripts.eval.fre630_extraction_quality.relabel_v2_types import V2_TYPE_DEFINITIONS

    assert V2_TYPE_DEFINITIONS.keys() == ALLOWED_ENTITY_TYPES_V2


def test_classification_prompt_states_ten_types() -> None:
    """The classification prompt's stated type count is 10, not the stale 8.

    AC-8 names this explicitly: "a stale eight-type prompt ... fails the
    assertion."
    """
    from scripts.eval.fre630_extraction_quality.relabel_v2_types import _classification_prompt

    prompt = _classification_prompt("<ENTITY>", "<CONTEXT>")
    assert "10 types" in prompt
    assert "8 types" not in prompt
    assert "one of the 10 keys" in prompt
