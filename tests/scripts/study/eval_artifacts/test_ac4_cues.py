"""Tests for the AC-4 abstract-cue gold artifact builder (FRE-841).

Candidate-pool generation/merging and artifact assembly are pure Python —
no fake Neo4j driver or real embedder/Agent-tool call needed. Only
`fetch_entities_for_candidate_pool` talks to Neo4j (thin boundary, tested
with a minimal fake session mirroring `test_writer.py`'s pattern).

The two-pass blind annotation itself (the `Agent`-tool dispatches +
adjudication) happens outside this module, in the build session — it is
not something a Python script can invoke — so `build_ac4_artifact` takes
already-annotated `CueAnnotationResult`s as its input, never an LLM/Agent
callback.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from scripts.study.eval_artifacts.ac4_cues import (
    ABSTRACT_CUES,
    AbstractCue,
    CandidateEntity,
    CueAnnotationResult,
    EntityWithOptionalEmbedding,
    build_ac4_artifact,
    build_embedding_candidates,
    build_keyword_candidates,
    compute_disagreements,
    fetch_entities_for_candidate_pool,
    merge_candidate_pools,
)


def _entity(
    name: str, entity_type: str, entity_id: str, embedding: list[float] | None = None
) -> EntityWithOptionalEmbedding:
    return EntityWithOptionalEmbedding(
        name=name, entity_type=entity_type, entity_id=entity_id, embedding=embedding
    )


# ---------------------------------------------------------------------------
# ABSTRACT_CUES constant
# ---------------------------------------------------------------------------


def test_abstract_cues_has_at_least_thirty() -> None:
    assert len(ABSTRACT_CUES) >= 30


def test_abstract_cues_span_at_least_four_domains() -> None:
    domains = {cue.domain for cue in ABSTRACT_CUES}
    assert len(domains) >= 4


def test_abstract_cues_are_unique() -> None:
    texts = [cue.cue_text for cue in ABSTRACT_CUES]
    assert len(texts) == len(set(texts))


def test_abstract_cues_each_have_keywords() -> None:
    assert all(len(cue.keywords) >= 3 for cue in ABSTRACT_CUES)


# ---------------------------------------------------------------------------
# build_embedding_candidates
# ---------------------------------------------------------------------------


def test_build_embedding_candidates_ranks_by_cosine_similarity() -> None:
    entities = [
        _entity("Close Match", "MethodOrConcept", "id1", embedding=[1.0, 0.0]),
        _entity("Far Match", "MethodOrConcept", "id2", embedding=[0.0, 1.0]),
        _entity("No Embedding", "MethodOrConcept", "id3", embedding=None),
    ]

    candidates = build_embedding_candidates([1.0, 0.0], entities, top_k=2)

    assert [c.entity_id for c in candidates] == ["id1", "id2"]
    assert all(c.pool_source == "embedding" for c in candidates)


def test_build_embedding_candidates_respects_top_k() -> None:
    entities = [_entity(f"Entity {i}", "Event", f"id{i}", embedding=[1.0, 0.0]) for i in range(10)]

    candidates = build_embedding_candidates([1.0, 0.0], entities, top_k=3)

    assert len(candidates) == 3


# ---------------------------------------------------------------------------
# build_keyword_candidates
# ---------------------------------------------------------------------------


def test_build_keyword_candidates_matches_case_insensitive_substring() -> None:
    cue = AbstractCue(cue_text="health issues", domain="health", keywords=("health", "clinical"))
    entities = [
        _entity("Health Status Reporting", "DomainOrTopic", "id1"),
        _entity("Respiratory infection", "Phenomenon", "id2"),
        _entity("Unrelated Thing", "Event", "id3"),
    ]

    candidates = build_keyword_candidates(cue, entities, max_candidates=20)

    assert {c.entity_id for c in candidates} == {"id1"}
    assert candidates[0].pool_source == "keyword"


def test_build_keyword_candidates_respects_max_candidates() -> None:
    cue = AbstractCue(cue_text="x", domain="d", keywords=("match",))
    entities = [_entity(f"Match {i}", "Event", f"id{i}") for i in range(10)]

    candidates = build_keyword_candidates(cue, entities, max_candidates=4)

    assert len(candidates) == 4


# ---------------------------------------------------------------------------
# merge_candidate_pools
# ---------------------------------------------------------------------------


def test_merge_candidate_pools_dedupes_and_tags_both() -> None:
    embedding_candidates = [
        CandidateEntity(entity_id="id1", name="A", kind="Event", pool_source="embedding"),
        CandidateEntity(entity_id="id2", name="B", kind="Event", pool_source="embedding"),
    ]
    keyword_candidates = [
        CandidateEntity(entity_id="id2", name="B", kind="Event", pool_source="keyword"),
        CandidateEntity(entity_id="id3", name="C", kind="Event", pool_source="keyword"),
    ]

    merged = merge_candidate_pools(embedding_candidates, keyword_candidates)
    by_id = {c.entity_id: c for c in merged}

    assert by_id["id1"].pool_source == "embedding"
    assert by_id["id2"].pool_source == "both"
    assert by_id["id3"].pool_source == "keyword"
    assert len(merged) == 3


# ---------------------------------------------------------------------------
# compute_disagreements
# ---------------------------------------------------------------------------


def test_compute_disagreements_finds_differing_labels() -> None:
    labels_1 = {"id1": "gold", "id2": "distractor"}
    labels_2 = {"id1": "gold", "id2": "gold"}

    disagreements = compute_disagreements(labels_1, labels_2)

    assert disagreements == ["id2"]


def test_compute_disagreements_empty_when_labels_match() -> None:
    labels_1 = {"id1": "gold"}
    labels_2 = {"id1": "gold"}

    assert compute_disagreements(labels_1, labels_2) == []


def test_compute_disagreements_flags_a_coverage_gap_as_a_disagreement() -> None:
    """An entity_id one annotator omitted entirely (not merely mislabeled) is
    still a disagreement — regression for a code-review finding (FRE-841)
    where iterating only labels_1's keys silently dropped this case.
    """
    labels_1 = {"id1": "gold"}
    labels_2 = {"id1": "gold", "id2": "gold"}

    assert compute_disagreements(labels_1, labels_2) == ["id2"]


# ---------------------------------------------------------------------------
# build_ac4_artifact
# ---------------------------------------------------------------------------


def test_build_ac4_artifact_assembles_cues_with_full_audit_trail() -> None:
    cue = AbstractCue(cue_text="health issues", domain="health", keywords=("health",))
    pool = [
        CandidateEntity(
            entity_id="id1",
            name="Health Status Reporting",
            kind="DomainOrTopic",
            pool_source="both",
        ),
        CandidateEntity(
            entity_id="id2", name="Unrelated Thing", kind="Event", pool_source="keyword"
        ),
    ]
    result = CueAnnotationResult(
        cue=cue,
        candidate_pool=pool,
        annotator_1_labels={"id1": "gold", "id2": "distractor"},
        annotator_2_labels={"id1": "gold", "id2": "distractor"},
        adjudications={},
        gold_neighborhood=("id1",),
        distractors=("id2",),
    )

    artifact = build_ac4_artifact([result], source_manifest_hash="deadbeef")

    assert artifact["source_manifest_hash"] == "deadbeef"
    assert "scoring_note" in artifact
    assert len(artifact["cues"]) == 1
    cue_entry = artifact["cues"][0]
    assert cue_entry["cue_text"] == "health issues"
    assert cue_entry["domain"] == "health"
    assert cue_entry["keywords"] == ["health"]
    assert cue_entry["gold_neighborhood"] == ["id1"]
    assert cue_entry["distractors"] == ["id2"]
    assert cue_entry["annotator_1_labels"] == {"id1": "gold", "id2": "distractor"}
    assert cue_entry["annotator_2_labels"] == {"id1": "gold", "id2": "distractor"}
    assert cue_entry["disagreements"] == []
    assert len(cue_entry["candidate_pool"]) == 2


def test_build_ac4_artifact_records_disagreements_and_adjudications() -> None:
    cue = AbstractCue(cue_text="health issues", domain="health", keywords=("health",))
    pool = [
        CandidateEntity(
            entity_id="id1", name="Ambiguous Thing", kind="DomainOrTopic", pool_source="embedding"
        ),
    ]
    result = CueAnnotationResult(
        cue=cue,
        candidate_pool=pool,
        annotator_1_labels={"id1": "gold"},
        annotator_2_labels={"id1": "distractor"},
        adjudications={"id1": "adjudicated gold — genuinely on-topic per manual inspection"},
        gold_neighborhood=("id1",),
        distractors=(),
    )

    artifact = build_ac4_artifact([result], source_manifest_hash=None)

    cue_entry = artifact["cues"][0]
    assert cue_entry["disagreements"] == ["id1"]
    assert cue_entry["adjudications"] == {
        "id1": "adjudicated gold — genuinely on-topic per manual inspection"
    }


# ---------------------------------------------------------------------------
# fetch_entities_for_candidate_pool (thin driver boundary)
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
async def test_fetch_entities_for_candidate_pool_maps_records_with_optional_embedding() -> None:
    driver = _FakeDriver(
        [
            {"name": "Foo", "entity_type": "Event", "entity_id": "id1", "embedding": [1.0, 2.0]},
            {"name": "Bar", "entity_type": "Person", "entity_id": "id2", "embedding": None},
        ]
    )

    entities = await fetch_entities_for_candidate_pool(driver)

    assert entities == [
        EntityWithOptionalEmbedding(
            name="Foo", entity_type="Event", entity_id="id1", embedding=[1.0, 2.0]
        ),
        EntityWithOptionalEmbedding(
            name="Bar", entity_type="Person", entity_id="id2", embedding=None
        ),
    ]
