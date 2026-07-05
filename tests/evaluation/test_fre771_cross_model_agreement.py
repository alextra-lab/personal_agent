"""FRE-771 — cross-model type-agreement over the `type-boundary` gold set (ADR-0109 AC-1).

Pure: no LLM, no substrate. Hand-computed extraction dicts stand in for real per-model
extractor output. AC-1 asks whether two model families *agree with each other* on an
entity's type — a different question from ``entity_type_accuracy`` (which asks whether
either agrees with a fixed gold label) — so this reuses `iaa.py`'s pairwise-agreement
machinery over per-model resolved type labels instead of duplicating statistics.
"""

from __future__ import annotations

from scripts.eval.fre630_extraction_quality.cross_model_agreement import (
    build_cross_model_agreement,
    type_boundary_cases,
)
from scripts.eval.fre630_extraction_quality.gold import GoldCase, GoldEntity


def _case(case_id: str, tags: tuple[str, ...], entity_name: str) -> GoldCase:
    return GoldCase(
        case_id=case_id,
        tags=tags,
        source_user=f"discussing {entity_name}",
        source_assistant="ok",
        expect_entities=(
            GoldEntity(name=entity_name, entity_type="Concept", knowledge_class="World"),
        ),
        expect_relationships=(),
    )


def _result(entity_name: str, extracted_type: str) -> dict:
    return {
        "entities": [{"name": entity_name, "type": extracted_type, "class": "World"}],
        "relationships": [],
        "stances": [],
        "claims": [],
    }


class TestTypeBoundaryCases:
    def test_filters_to_tagged_cases_only(self) -> None:
        cases = [
            _case("boundary-1", ("type-boundary",), "GraphRAG"),
            _case("plain-1", ("residence-vs-visit",), "Torcello"),
            _case("boundary-2", ("type-boundary", "cs"), "Regex"),
        ]
        selected = type_boundary_cases(cases)
        assert [c.case_id for c in selected] == ["boundary-1", "boundary-2"]


class TestBuildCrossModelAgreement:
    def test_two_models_full_agreement(self) -> None:
        """Both model families emit the same type for every boundary entity → agreement 1.0."""
        cases = [_case("boundary-1", ("type-boundary",), "GraphRAG")]
        results_by_model = {
            "mini": {"boundary-1": _result("GraphRAG", "MethodOrConcept")},
            "sonnet": {"boundary-1": _result("GraphRAG", "MethodOrConcept")},
        }
        report = build_cross_model_agreement(cases, results_by_model)
        assert report.n_items == 1
        assert report.overall_agreement == 1.0
        assert report.by_pair[("mini", "sonnet")] == 1.0
        assert report.disagreements == ()

    def test_two_models_disagree_on_one_of_two(self) -> None:
        """One item disagrees, one agrees → overall agreement 0.5."""
        cases = [
            _case("boundary-1", ("type-boundary",), "GraphRAG"),
            _case("boundary-2", ("type-boundary",), "Behavioral Economics"),
        ]
        results_by_model = {
            "mini": {
                "boundary-1": _result("GraphRAG", "MethodOrConcept"),
                "boundary-2": _result("Behavioral Economics", "DomainOrTopic"),
            },
            "sonnet": {
                "boundary-1": _result("GraphRAG", "MethodOrConcept"),
                "boundary-2": _result("Behavioral Economics", "MethodOrConcept"),
            },
        }
        report = build_cross_model_agreement(cases, results_by_model)
        assert report.n_items == 2
        assert report.overall_agreement == 0.5
        assert report.disagreements == ("boundary-2::Behavioral Economics",)

    def test_unresolved_entity_excluded_not_counted_as_disagreement(self) -> None:
        """A model that fails to extract the entity at all drops the item, not a mismatch."""
        cases = [_case("boundary-1", ("type-boundary",), "GraphRAG")]
        results_by_model = {
            "mini": {"boundary-1": _result("GraphRAG", "MethodOrConcept")},
            "sonnet": {
                "boundary-1": {"entities": [], "relationships": [], "stances": [], "claims": []}
            },
        }
        report = build_cross_model_agreement(cases, results_by_model)
        assert report.n_items == 0
        assert report.overall_agreement is None

    def test_non_boundary_cases_ignored(self) -> None:
        """Only `type-boundary`-tagged cases feed the agreement calculation."""
        cases = [_case("plain-1", ("residence-vs-visit",), "Torcello")]
        results_by_model = {
            "mini": {"plain-1": _result("Torcello", "Location")},
            "sonnet": {"plain-1": _result("Torcello", "MethodOrConcept")},
        }
        report = build_cross_model_agreement(cases, results_by_model)
        assert report.n_items == 0
        assert report.overall_agreement is None
