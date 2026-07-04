"""FRE-630 — pure core unit tests: matcher tiers, metrics, and case scoring.

Fully deterministic, no LLM, no substrate. This is the AC-1 proof that the
extraction-quality instrument's scoring core is correct.
"""

from __future__ import annotations

import pytest
from scripts.eval.fre630_extraction_quality.gold import (
    GoldCase,
    GoldClaim,
    GoldEntity,
    GoldRelationship,
    GoldStance,
)
from scripts.eval.fre630_extraction_quality.matching import (
    match_entities,
    matches_any,
    normalize_name,
)
from scripts.eval.fre630_extraction_quality.metrics import (
    ExtractedRel,
    claim_case_level_recall,
    claim_emission_recall,
    dedup_convergence,
    description_integrity,
    entity_prf,
    entity_type_accuracy,
    extraction_empty,
    forbidden_edge_type_rate,
    hallucination_rate,
    knowledge_class_accuracy,
    mean_optional,
    mean_std,
    relationship_prf,
    relationship_type_correctness,
    stance_emission_recall,
)
from scripts.eval.fre630_extraction_quality.scoring import parse_extraction, score_case


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Neo4j", "neo4j"),
        ("  Game   Theory  ", "game theory"),
        ("Météo France", "meteo france"),
        ("Python.", "python"),
        ("(Consciousness)", "consciousness"),
        ("", ""),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    """Normalization case-folds, collapses whitespace, strips punctuation, folds accents."""
    assert normalize_name(raw) == expected


def _ent(name: str, aliases: tuple[str, ...] = ()) -> GoldEntity:
    return GoldEntity(name=name, entity_type="Concept", knowledge_class="World", aliases=aliases)


def test_match_exact_after_normalization() -> None:
    """A normalized surface form matches its gold entity in the exact tier."""
    result = match_entities([_ent("Neo4j")], ["neo4j"])
    assert len(result.matches) == 1
    assert result.matches[0].tier == "exact"
    assert result.matches[0].gold_name == "Neo4j"
    assert not result.unmatched_gold
    assert not result.unmatched_extracted


def test_match_alias_tier() -> None:
    """An accepted alias matches in the alias tier."""
    result = match_entities([_ent("Game Theory", aliases=("GT",))], ["gt"])
    assert len(result.matches) == 1
    assert result.matches[0].tier == "alias"


def test_match_fuzzy_tier() -> None:
    """A near-miss typo matches in the fuzzy tier."""
    result = match_entities([_ent("Neuroplasticity")], ["Neuroplasticty"])
    assert len(result.matches) == 1
    assert result.matches[0].tier == "fuzzy"


def test_exact_beats_fuzzy_and_each_extracted_claimed_once() -> None:
    """An exact match is never displaced by a competing fuzzy match; each name claimed once."""
    gold = [_ent("Neuroplasticity"), _ent("Plasticity")]
    result = match_entities(gold, ["Plasticity"])
    matched = {m.gold_name: m.tier for m in result.matches}
    assert matched.get("Plasticity") == "exact"
    assert "Neuroplasticity" in result.unmatched_gold


def test_unmatched_partitions() -> None:
    """Unmatched gold and extracted names land in the right partitions."""
    result = match_entities([_ent("Alpha")], ["Beta"])
    assert result.unmatched_gold == ("Alpha",)
    assert result.unmatched_extracted == ("Beta",)


def test_match_is_deterministic() -> None:
    """Matching is order-independent for the resolved gold→extracted map."""
    gold = [_ent("Consciousness"), _ent("Consciousness and AI")]
    a = match_entities(gold, ["consciousness", "consciousness and ai"])
    b = match_entities(gold, ["consciousness and ai", "consciousness"])
    assert a.gold_to_extracted() == b.gold_to_extracted()


def test_matches_any_uses_exact_alias_only() -> None:
    """The trap matcher fires on a normalized surface hit, not a fuzzy near-miss."""
    assert matches_any("DISCUSES", ["discuses"])
    assert not matches_any("Neo4j", ["Postgres"])


def test_entity_prf_perfect() -> None:
    """A complete match yields precision/recall/F1 of 1.0."""
    match = match_entities([_ent("A"), _ent("B")], ["A", "B"])
    prf = entity_prf(match)
    assert prf.precision == 1.0
    assert prf.recall == 1.0
    assert prf.f1 == 1.0
    assert (prf.tp, prf.fp, prf.fn) == (2, 0, 0)


def test_entity_prf_mixed() -> None:
    """A partial match yields the expected confusion counts and rates."""
    match = match_entities([_ent("A"), _ent("B"), _ent("C")], ["A", "X"])
    prf = entity_prf(match)
    assert prf.tp == 1 and prf.fp == 1 and prf.fn == 2
    assert prf.precision == 0.5
    assert prf.recall == pytest.approx(1 / 3)


def test_type_and_class_accuracy_over_matches() -> None:
    """Type/class accuracy is computed over matched entities only."""
    gold = [
        GoldEntity(name="Torcello", entity_type="Location", knowledge_class="World"),
        GoldEntity(name="Python", entity_type="Technology", knowledge_class="World"),
    ]
    match = match_entities(gold, ["Torcello", "Python"])
    gold_types = {"Torcello": "Location", "Python": "Technology"}
    gold_classes = {"Torcello": "World", "Python": "World"}
    ext_types = {"Torcello": "Location", "Python": "Concept"}  # Python mis-typed
    ext_classes = {"Torcello": "World", "Python": "World"}
    assert entity_type_accuracy(match, gold_types, ext_types) == 0.5
    assert knowledge_class_accuracy(match, gold_classes, ext_classes) == 1.0


def test_accuracy_none_when_no_matches() -> None:
    """Accuracy is None when nothing matched (excluded from aggregates)."""
    match = match_entities([_ent("A")], ["Z"])
    assert entity_type_accuracy(match, {"A": "Concept"}, {}) is None


def test_relationship_prf_resolves_endpoints() -> None:
    """An edge scores as a true positive when its endpoints resolve, even via fuzzy match."""
    gold_entities = [_ent("Python"), _ent("FastAPI")]
    match = match_entities(gold_entities, ["Python", "Fast API"])
    gold_rels = [GoldRelationship(source="FastAPI", rel_type="USES", target="Python")]
    ext_rels = [ExtractedRel(source="Fast API", rel_type="USES", target="Python")]
    prf = relationship_prf(gold_rels, ext_rels, match)
    assert (prf.tp, prf.fp, prf.fn) == (1, 0, 0)


def test_relationship_prf_unresolved_endpoint_is_false_positive() -> None:
    """An edge whose endpoint does not resolve to a gold entity is a false positive."""
    match = match_entities([_ent("Python")], ["Python"])
    ext_rels = [ExtractedRel(source="Ghost", rel_type="USES", target="Python")]
    prf = relationship_prf([], ext_rels, match)
    assert prf.tp == 0 and prf.fp == 1


def test_relationship_type_correctness_residence_vs_visit() -> None:
    """Right endpoints with the wrong edge type score 0 on type-correctness."""
    gold_entities = [_ent("Owner"), _ent("Torcello")]
    match = match_entities(gold_entities, ["Owner", "Torcello"])
    gold_rels = [GoldRelationship(source="Owner", rel_type="RELATED_TO", target="Torcello")]
    ext_rels = [ExtractedRel(source="Owner", rel_type="LOCATED_IN", target="Torcello")]
    assert relationship_type_correctness(gold_rels, ext_rels, match) == 0.0


def test_relationship_type_correctness_none_when_no_aligned_edges() -> None:
    """Type-correctness is None when no extracted edge lands on a gold endpoint pair."""
    match = match_entities([_ent("A")], ["A"])
    assert relationship_type_correctness([], [], match) is None


def test_hallucination_rate() -> None:
    """Hallucination rate is trap-hits over total extracted, None when nothing extracted."""
    assert hallucination_rate(["Python", "DISCUSES"], ["discuses"]) == 0.5
    assert hallucination_rate(["Python"], []) == 0.0
    assert hallucination_rate([], ["x"]) is None


def test_forbidden_edge_type_rate() -> None:
    """Off-vocabulary and case-forbidden edge types are counted; None when no edges."""
    rels = [
        ExtractedRel("A", "USES", "B"),  # in vocab, allowed
        ExtractedRel("A", "LIVES_IN", "B"),  # off-vocab
    ]
    assert forbidden_edge_type_rate(rels, []) == 0.5
    assert forbidden_edge_type_rate([ExtractedRel("A", "LOCATED_IN", "B")], ["LOCATED_IN"]) == 1.0
    assert forbidden_edge_type_rate([], []) is None


def test_extraction_empty() -> None:
    """Empty extraction against a positive-labeled case is flagged; otherwise not."""
    assert extraction_empty(0, gold_has_positive=True) is True
    assert extraction_empty(0, gold_has_positive=False) is False
    assert extraction_empty(3, gold_has_positive=True) is False


def test_dedup_convergence() -> None:
    """Both variants present → not collapsed; a single surface form → collapsed."""
    assert (
        dedup_convergence(["Game Theory", "Game theory"], [("Game Theory", "Game theory")]) == 0.0
    )
    assert dedup_convergence(["Game Theory"], [("Game Theory", "Game theory")]) == 1.0
    assert dedup_convergence(["x"], []) is None


def test_description_integrity_proxy() -> None:
    """The proxy passes clean single sentences and fails multi-sentence / stance-flatten."""
    good = ["A concrete single sentence about scattering."]
    flattened = ["A drivetrain concept the user likes strongly."]
    multi = ["First sentence. Second sentence."]
    assert description_integrity(good) == 1.0
    assert description_integrity(flattened) == 0.0
    assert description_integrity(multi) == 0.0
    assert description_integrity([]) is None


def test_stance_and_claim_emission_recall() -> None:
    """Stance/claim emission recall matches by target/facet; facetless claims are excluded."""
    stances = [GoldStance(target="Manual Transmission"), GoldStance(target="Jazz")]
    assert stance_emission_recall(stances, ["manual transmission"]) == 0.5
    assert stance_emission_recall([], []) is None
    claims = [
        GoldClaim(facet="lease_end_date", content_gist="lease expires soon"),
        GoldClaim(facet="", content_gist="free-form"),
    ]
    assert claim_emission_recall(claims, ["lease_end_date"]) == 1.0  # facetless excluded
    assert claim_emission_recall([GoldClaim(facet="", content_gist="x")], []) is None


def test_mean_optional_and_mean_std() -> None:
    """Aggregation helpers exclude None and report mean/std over present values."""
    assert mean_optional([1.0, None, 3.0]) == 2.0
    assert mean_optional([None, None]) is None
    ms = mean_std([1.0, 1.0, 1.0])
    assert ms.mean == 1.0 and ms.std == 0.0 and ms.n == 3
    ms_single = mean_std([0.5, None])
    assert ms_single.mean == 0.5 and ms_single.std is None and ms_single.n == 1
    assert mean_std([]).mean is None


def test_parse_extraction_tolerates_missing_keys() -> None:
    """Parsing an empty dict yields an empty-fallback view without raising."""
    parsed = parse_extraction({})
    assert parsed.entity_names == ()
    assert parsed.is_empty_fallback is True


def _full_case() -> GoldCase:
    return GoldCase(
        case_id="visit-not-residence",
        tags=("residence-vs-visit", "travel"),
        source_user="I was in Torcello last week.",
        source_assistant="Torcello is an island in the Venetian lagoon.",
        expect_entities=(
            GoldEntity(name="Torcello", entity_type="Location", knowledge_class="World"),
        ),
        expect_relationships=(),
        forbid_entities=("DISCUSES",),
        forbid_rel_types=("LIVES_IN", "LOCATED_IN"),
    )


def test_score_case_end_to_end_clean() -> None:
    """A clean extraction scores perfectly with no diffs."""
    case = _full_case()
    result = {
        "entities": [
            {
                "name": "Torcello",
                "type": "Location",
                "class": "World",
                "description": "An island in the Venetian lagoon.",
            }
        ],
        "relationships": [],
        "stances": [],
        "claims": [],
    }
    score = score_case(case, result, entity_type_field="v2")
    assert score.entity.precision == 1.0
    assert score.entity_type_accuracy == 1.0
    assert score.hallucination_rate == 0.0
    assert score.is_empty_fallback is False
    assert score.diffs == {}


def test_score_case_end_to_end_with_failures() -> None:
    """A failing extraction surfaces the trap metrics and per-case diffs."""
    case = _full_case()
    result = {
        "entities": [
            {
                "name": "Torcello",
                "type": "Concept",
                "class": "Personal",
                "description": "A place the user likes strongly.",
            },
            {
                "name": "DISCUSES",
                "type": "Concept",
                "class": "System",
                "description": "leaked rel type.",
            },
        ],
        "relationships": [
            {"source": "Owner", "type": "LIVES_IN", "target": "Torcello"},
        ],
        "stances": [],
        "claims": [],
    }
    score = score_case(case, result, entity_type_field="v2")
    assert score.entity_type_accuracy == 0.0  # Torcello mis-typed Concept
    assert score.knowledge_class_accuracy == 0.0  # mis-classed Personal
    assert score.hallucination_rate == 0.5  # DISCUSES
    assert score.forbidden_edge_type_rate == 1.0  # LIVES_IN
    # Torcello's description flattens a stance (fails); DISCUSES's is a clean sentence
    # (passes) — the proxy scores each description independently → 1 of 2.
    assert score.description_integrity == 0.5
    assert "hallucinated" in score.diffs
    assert "wrong_type" in score.diffs
    assert "forbidden_edges" in score.diffs


class TestClaimCaseLevelRecall:
    """FRE-759 (codex P1.1): case-level claim recall over DISTINCT claim cases.

    Guards against the sample-flattening trap — with --samples 3, six claim cases
    must not read as n=18. A case passes iff the MAJORITY of its samples emitted
    the expected claim(s) (mean per-sample recall >= min_recall, default 0.5).
    """

    def test_none_when_no_claim_cases(self) -> None:
        """No claim-bearing case → fraction is None (excluded, never a misleading 1.0)."""
        result = claim_case_level_recall([[None, None], [None]])
        assert result.total == 0
        assert result.passing == 0
        assert result.fraction is None

    def test_counts_distinct_cases_not_samples(self) -> None:
        """Two distinct claim cases → total=2 regardless of sample count."""
        # case A: emitted in 3/3 samples (pass); case B: 0/3 (fail).
        result = claim_case_level_recall([[1.0, 1.0, 1.0], [0.0, 0.0, 0.0]])
        assert result.total == 2
        assert result.passing == 1
        assert result.fraction == 0.5

    def test_majority_of_samples_decides_a_case(self) -> None:
        """A case passes on 2/3 samples (mean 0.67 ≥ 0.5), fails on 1/3 (mean 0.33)."""
        passes = claim_case_level_recall([[1.0, 1.0, 0.0]])
        assert passes.passing == 1 and passes.total == 1 and passes.fraction == 1.0
        fails = claim_case_level_recall([[1.0, 0.0, 0.0]])
        assert fails.passing == 0 and fails.total == 1 and fails.fraction == 0.0

    def test_non_claim_samples_excluded_from_a_mixed_case(self) -> None:
        """A case with some None samples averages only its present recalls."""
        # present recalls are 1.0, 1.0 → mean 1.0 ≥ 0.5 → pass; the None is ignored.
        result = claim_case_level_recall([[1.0, None, 1.0]])
        assert result.total == 1 and result.passing == 1

    def test_eight_of_ten_reads_as_point_eight(self) -> None:
        """The AC-2 shape: 8 of 10 distinct claim cases passing → fraction 0.8."""
        cases = [[1.0, 1.0, 1.0]] * 8 + [[0.0, 0.0, 0.0]] * 2
        result = claim_case_level_recall(cases)
        assert result.total == 10 and result.passing == 8
        assert result.fraction == 0.8


# ── FRE-771: entity_type_field selects which gold type score_case scores against ──


def _boundary_case() -> GoldCase:
    """A single-entity case with distinct V1 and V2 gold type labels."""
    return GoldCase(
        case_id="fre771-boundary",
        tags=("type-boundary",),
        source_user="GraphRAG combines retrieval with generation.",
        source_assistant="It is a well-known technique.",
        expect_entities=(
            GoldEntity(
                name="GraphRAG",
                entity_type="Concept",
                knowledge_class="World",
                v2_type="MethodOrConcept",
            ),
        ),
        expect_relationships=(),
    )


def test_score_case_entity_type_field_v2_scores_against_v2_type() -> None:
    """entity_type_field='v2' matches an extraction emitting the V2 label."""
    case = _boundary_case()
    result = {
        "entities": [{"name": "GraphRAG", "type": "MethodOrConcept", "class": "World"}],
        "relationships": [],
        "stances": [],
        "claims": [],
    }
    score_v2 = score_case(case, result, entity_type_field="v2")
    assert score_v2.entity_type_accuracy == 1.0
    score_v1 = score_case(case, result, entity_type_field="v1")
    assert score_v1.entity_type_accuracy == 0.0


def test_score_case_entity_type_field_v1_scores_against_v1_type() -> None:
    """entity_type_field='v1' matches an extraction emitting the retired V1 label."""
    case = _boundary_case()
    result = {
        "entities": [{"name": "GraphRAG", "type": "Concept", "class": "World"}],
        "relationships": [],
        "stances": [],
        "claims": [],
    }
    score_v1 = score_case(case, result, entity_type_field="v1")
    assert score_v1.entity_type_accuracy == 1.0
    score_v2 = score_case(case, result, entity_type_field="v2")
    assert score_v2.entity_type_accuracy == 0.0
