"""Tests for the AC-2 hard-negative artifact builder (FRE-841).

V+ mining/pairing (`group_by_normalizer`/`expand_to_pairs`/`build_positive_pairs`)
is pure Python over plain `EntityRecord` lists — no fake Neo4j driver needed;
only `fetch_all_entities` talks to Neo4j, tested separately with a minimal
fake session (mirrors `test_writer.py`'s `_ScriptedSession`/`_FakeResult`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from scripts.study.eval_artifacts.ac2_pairs import (
    SEEDED_HARD_NEGATIVE_PAIRS,
    EntityRecord,
    build_ac2_artifact,
    build_positive_pairs,
    expand_to_pairs,
    fetch_all_entities,
    group_by_normalizer,
    resolve_seeded_pair,
)


def _e(name: str, entity_type: str, entity_id: str) -> EntityRecord:
    return EntityRecord(name=name, entity_type=entity_type, entity_id=entity_id)


# ---------------------------------------------------------------------------
# group_by_normalizer / expand_to_pairs
# ---------------------------------------------------------------------------


def test_group_by_normalizer_groups_case_variants() -> None:
    entities = [
        _e("Arterial calcification", "Phenomenon", "id1"),
        _e("Arterial Calcification", "MethodOrConcept", "id2"),
        _e("Unrelated Thing", "Event", "id3"),
    ]

    groups = group_by_normalizer(entities, str.lower, "corpus_case_variant")

    assert len(groups) == 1
    assert groups[0].normalized_name == "arterial calcification"
    assert groups[0].provenance == "corpus_case_variant"
    assert {m.name for m in groups[0].members} == {
        "Arterial calcification",
        "Arterial Calcification",
    }


def test_group_by_normalizer_excludes_singleton_groups() -> None:
    entities = [_e("Only One", "Event", "id1")]

    groups = group_by_normalizer(entities, str.lower, "corpus_case_variant")

    assert groups == []


def test_group_by_normalizer_dedupes_repeated_exact_names() -> None:
    """Two nodes with the identical raw name collapse to one member per name
    (deterministic: first-seen wins), so a group needs >=2 *distinct* raw
    names, not merely >=2 nodes.
    """
    entities = [
        _e("Same Name", "Event", "id1"),
        _e("Same Name", "Event", "id2"),
    ]

    groups = group_by_normalizer(entities, str.lower, "corpus_case_variant")

    assert groups == []


def test_expand_to_pairs_builds_all_combinations_within_a_group() -> None:
    entities = [
        _e("Agent logs", "DomainOrTopic", "id1"),
        _e("Agent Logs", "KnowledgeArtifact", "id2"),
        _e("agent logs", "DomainOrTopic", "id3"),
    ]
    groups = group_by_normalizer(entities, str.lower, "corpus_case_variant")

    pairs = expand_to_pairs(groups)

    assert len(pairs) == 3  # C(3,2)
    assert all(p.provenance == "corpus_case_variant" for p in pairs)


# ---------------------------------------------------------------------------
# build_positive_pairs (case + near-variant, deduped)
# ---------------------------------------------------------------------------


def test_build_positive_pairs_includes_case_variant_pairs() -> None:
    entities = [
        _e("Admin panel", "MethodOrConcept", "id1"),
        _e("Admin Panel", "MethodOrConcept", "id2"),
    ]

    pairs = build_positive_pairs(entities)

    assert len(pairs) == 1
    assert pairs[0].provenance == "corpus_case_variant"


def test_build_positive_pairs_near_variant_is_additive_not_duplicated() -> None:
    """A near-variant pair whose two names are ALSO plain case-variants of
    each other must not be double-counted under both provenances — the
    near-variant pass only contributes pairs the case-fold pass couldn't
    already find (codex plan-review finding #3).
    """
    entities = [
        _e("95th Percentile (P95)", "QuantityMeasure", "id1"),
        _e("95th percentile (P95)", "QuantityMeasure", "id2"),
    ]

    pairs = build_positive_pairs(entities)

    # These two names differ only by case -> already covered by the
    # case-variant pass; the near-variant (punctuation-stripped) pass must
    # not re-emit the same entity_id pair under a second provenance.
    assert len(pairs) == 1
    assert pairs[0].provenance == "corpus_case_variant"


def test_build_positive_pairs_near_variant_catches_punctuation_only_differences() -> None:
    entities = [
        _e("Agent-captains-captures", "TechnicalArtifact", "id1"),
        _e("Agent captains captures", "TechnicalArtifact", "id2"),
    ]

    pairs = build_positive_pairs(entities)

    assert len(pairs) == 1
    assert pairs[0].provenance == "corpus_near_variant"


# ---------------------------------------------------------------------------
# SEEDED_HARD_NEGATIVE_PAIRS / resolve_seeded_pair
# ---------------------------------------------------------------------------


def test_seeded_hard_negative_pairs_has_at_least_ten_entries() -> None:
    assert len(SEEDED_HARD_NEGATIVE_PAIRS) >= 10


def test_seeded_hard_negative_pairs_includes_adr_named_examples() -> None:
    surfaces = {(p.surface_a.lower(), p.surface_b.lower()) for p in SEEDED_HARD_NEGATIVE_PAIRS}
    assert ("python", "python") in surfaces
    assert ("apple", "apple") in surfaces


def test_resolve_seeded_pair_marks_corpus_attested_one_side() -> None:
    from scripts.study.eval_artifacts.ac2_pairs import SeededPairSpec

    spec = SeededPairSpec(
        surface_a="Python",
        sense_a="the programming language",
        kind_hint_a="TechnicalArtifact",
        surface_b="python",
        sense_b="the snake genus",
        kind_hint_b="DomainOrTopic",
    )
    entities = [_e("Python", "TechnicalArtifact", "real-id-1")]

    resolved = resolve_seeded_pair(spec, entities)

    assert resolved.provenance == "corpus_attested_one_side"
    assert resolved.entity_id_a == "real-id-1"
    assert resolved.kind_a == "TechnicalArtifact"
    assert resolved.entity_id_b is None
    assert resolved.kind_b == "DomainOrTopic"
    assert resolved.scoring_note  # non-empty


def test_resolve_seeded_pair_marks_same_surface_ambiguous_not_both_sides() -> None:
    """A byte-identical pair (surface_a == surface_b) can only ever match ONE
    real corpus node via name lookup — that must not be reported as "both
    sides attested" (which would wrongly imply two distinct real entities).
    """
    from scripts.study.eval_artifacts.ac2_pairs import SeededPairSpec

    spec = SeededPairSpec(
        surface_a="Mercury",
        sense_a="the planet",
        kind_hint_a="DomainOrTopic",
        surface_b="Mercury",
        sense_b="a mail-client software product",
        kind_hint_b="TechnicalArtifact",
    )
    entities = [_e("Mercury", "DomainOrTopic", "real-id-1")]

    resolved = resolve_seeded_pair(spec, entities)

    assert resolved.provenance == "corpus_attested_same_surface_ambiguous"
    assert resolved.entity_id_a == "real-id-1"
    assert resolved.entity_id_b == "real-id-1"
    assert "entity_id_a to entity_id_b" in resolved.scoring_note
    # Regression (code-review finding, FRE-841): a name-based lookup for a
    # byte-identical surface necessarily resolves match_b to the SAME corpus
    # node as match_a — kind_b must stay the intended kind_hint_b (the second
    # sense's hint), not silently collapse to match_a's real corpus kind.
    assert resolved.kind_a == "DomainOrTopic"
    assert resolved.kind_b == "TechnicalArtifact"


def test_resolve_seeded_pair_marks_fully_synthetic_when_neither_side_attested() -> None:
    from scripts.study.eval_artifacts.ac2_pairs import SeededPairSpec

    spec = SeededPairSpec(
        surface_a="Zzyzx",
        sense_a="sense one",
        kind_hint_a="Event",
        surface_b="zzyzx",
        sense_b="sense two",
        kind_hint_b="Event",
    )

    resolved = resolve_seeded_pair(spec, entities=[])

    assert resolved.provenance == "fully_synthetic"
    assert resolved.entity_id_a is None
    assert resolved.entity_id_b is None


# ---------------------------------------------------------------------------
# fetch_all_entities (thin driver boundary)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[dict[str, Any]]:
        for record in self._records:
            yield record

    async def single(self) -> dict[str, Any] | None:
        return self._records[0] if self._records else None


class _FakeSession:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        return _FakeResult(self._records)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeDriver:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def session(self) -> _FakeSession:
        return _FakeSession(self._records)


@pytest.mark.asyncio
async def test_fetch_all_entities_maps_records() -> None:
    driver = _FakeDriver(
        [
            {"name": "Foo", "entity_type": "Event", "entity_id": "id1"},
            {"name": "Bar", "entity_type": "Person", "entity_id": "id2"},
        ]
    )

    entities = await fetch_all_entities(driver)

    assert entities == [
        EntityRecord(name="Foo", entity_type="Event", entity_id="id1"),
        EntityRecord(name="Bar", entity_type="Person", entity_id="id2"),
    ]


# ---------------------------------------------------------------------------
# build_ac2_artifact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_ac2_artifact_assembles_positive_and_negative_pairs() -> None:
    driver = _FakeDriver(
        [
            {"name": "Arterial calcification", "entity_type": "Phenomenon", "entity_id": "id1"},
            {
                "name": "Arterial Calcification",
                "entity_type": "MethodOrConcept",
                "entity_id": "id2",
            },
        ]
    )

    artifact = await build_ac2_artifact(driver, source_manifest_hash="deadbeef")

    assert len(artifact["positive_pairs"]) == 1
    assert len(artifact["negative_pairs"]) == len(SEEDED_HARD_NEGATIVE_PAIRS)
    assert artifact["source_manifest_hash"] == "deadbeef"
