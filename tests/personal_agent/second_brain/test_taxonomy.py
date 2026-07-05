"""Invariants for the ADR-0109 entity-type taxonomy single-source module (FRE-772)."""

from __future__ import annotations

from personal_agent.second_brain import taxonomy


def test_v1_has_seven_inherited_types() -> None:
    assert taxonomy.V1_ENTITY_TYPES == frozenset(
        {"Person", "Organization", "Location", "Technology", "Concept", "Event", "Topic"}
    )


def test_v2_has_ten_types() -> None:
    assert len(taxonomy.V2_ENTITY_TYPES) == 10
    assert taxonomy.V2_ENTITY_TYPES == frozenset(
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


def test_deterministic_map_only_covers_changing_types() -> None:
    # The deterministic map holds exactly the V1 types whose string CHANGES under V2.
    assert taxonomy.V1_TO_V2_DETERMINISTIC == {
        "Technology": "TechnicalArtifact",
        "Topic": "DomainOrTopic",
    }
    # Every target is a real V2 type.
    for target in taxonomy.V1_TO_V2_DETERMINISTIC.values():
        assert target in taxonomy.V2_ENTITY_TYPES


def test_v1_partitions_cleanly_into_unchanged_deterministic_and_concept() -> None:
    # V1 = (types valid as-is in V2) ∪ (deterministically remapped) ∪ {Concept}, pairwise disjoint.
    unchanged = taxonomy.V1_ENTITY_TYPES & taxonomy.V2_ENTITY_TYPES
    changing = set(taxonomy.V1_TO_V2_DETERMINISTIC)
    assert unchanged == {"Person", "Organization", "Location", "Event"}
    assert unchanged.isdisjoint(changing)
    assert "Concept" not in unchanged and "Concept" not in changing
    assert unchanged | changing | {"Concept"} == taxonomy.V1_ENTITY_TYPES


def test_concept_target_set_is_the_five_conceptual_v2_types() -> None:
    assert taxonomy.V1_CONCEPT_TARGET_TYPES == frozenset(
        {
            "MethodOrConcept",
            "DomainOrTopic",
            "Phenomenon",
            "QuantityMeasure",
            "KnowledgeArtifact",
        }
    )
    assert taxonomy.V1_CONCEPT_TARGET_TYPES <= taxonomy.V2_ENTITY_TYPES


def test_v1_only_types_are_exactly_the_retired_three() -> None:
    assert taxonomy.V1_ENTITY_TYPES - taxonomy.V2_ENTITY_TYPES == {
        "Technology",
        "Concept",
        "Topic",
    }


def test_extractor_prompt_speaks_every_v2_type_drift_guard() -> None:
    # The live extractor prompt and this module must not silently diverge: every V2 type name
    # this migration writes must appear in the extractor's template (FRE-771 shipped exactly these).
    from personal_agent.second_brain import entity_extraction

    template = entity_extraction._EXTRACTION_PROMPT_TEMPLATE
    for type_name in taxonomy.V2_ENTITY_TYPES:
        assert type_name in template, f"{type_name} missing from the live extractor prompt"
