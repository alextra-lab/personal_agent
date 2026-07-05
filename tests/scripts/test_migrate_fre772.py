"""Unit tests for the FRE-772 entity-type migration orchestration.

These exercise the migration ALGORITHM against an in-memory fake graph + a deterministic fake
classifier — no Neo4j, no LLM, so they run in ``make test`` as the CI-gating AC-4 mechanism proof.
The real Cypher (:class:`_Neo4jGraph`) is exercised by ``test_migrate_fre772_integration.py``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

import pytest
from scripts.migrate_fre772_entity_type_v2 import (
    ClassifyResult,
    ConceptNode,
    _map_speaks_v2,
    _parse_classification,
    run_migration,
    run_rollback,
)

_RUN_ID = "fre772-test"
_NOW = "2026-07-05T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeGraph:
    """In-memory :class:`GraphProtocol` over a list of node dicts; counts every mutation."""

    def __init__(self, nodes: list[dict[str, str]]) -> None:
        self.nodes = nodes
        self.writes = 0

    async def count_by_type(self) -> dict[str, int]:
        return dict(Counter(n.get("entity_type", "") for n in self.nodes))

    async def snapshot(self) -> list[dict[str, str]]:
        return [
            {"name": n["name"], "entity_type": n.get("entity_type", "")}
            for n in self.nodes
            if n.get("name")
        ]

    async def remap_deterministic(
        self, v1: str, v2: str, *, run_id: str, now: str, batch: int
    ) -> int:
        count = 0
        for n in self.nodes:
            if n.get("entity_type") == v1:
                n["entity_type"] = v2
                n["entity_type_migration"] = run_id
                n["entity_type_migrated_at"] = now
                self.writes += 1
                count += 1
        return count

    async def fetch_concepts(self, cursor: str | None, limit: int) -> list[ConceptNode]:
        concepts = sorted(
            (n for n in self.nodes if n.get("entity_type") == "Concept"), key=lambda n: n["eid"]
        )
        if cursor is not None:
            concepts = [n for n in concepts if n["eid"] > cursor]
        return [
            ConceptNode(element_id=n["eid"], name=n["name"], description=n.get("description", ""))
            for n in concepts[:limit]
        ]

    async def set_entity_type(self, element_id: str, v2: str, *, run_id: str, now: str) -> None:
        for n in self.nodes:
            if n["eid"] == element_id:
                n["entity_type"] = v2
                n["entity_type_migration"] = run_id
                n["entity_type_migrated_at"] = now
                n.pop("entity_type_migration_error", None)
                self.writes += 1

    async def mark_error(self, element_id: str, reason: str, *, now: str) -> None:
        for n in self.nodes:
            if n["eid"] == element_id:
                n["entity_type_migration_error"] = reason
                self.writes += 1

    async def restore_types(self, rows: Sequence[dict[str, str]], *, batch: int) -> int:
        by_name = {r["name"]: r["entity_type"] for r in rows}
        count = 0
        for n in self.nodes:
            if n.get("name") in by_name:
                n["entity_type"] = by_name[n["name"]]
                for key in (
                    "entity_type_migration",
                    "entity_type_migrated_at",
                    "entity_type_migration_error",
                ):
                    n.pop(key, None)
                self.writes += 1
                count += 1
        return count


def _classifier(mapping: dict[str, str], *, cost: float = 0.0):
    """Return a fake classifier: name→type from ``mapping``, else fail-closed (None)."""

    async def classify(name: str, description: str) -> ClassifyResult:
        target = mapping.get(name)
        if target is None:
            return ClassifyResult(entity_type=None, cost_usd=cost, reason="out_of_set")
        return ClassifyResult(entity_type=target, cost_usd=cost)

    return classify


async def _run(graph: FakeGraph, classifier, **kw):
    return await run_migration(
        graph,
        classifier,
        run_id=_RUN_ID,
        now=_NOW,
        classifier_model="fake",
        **kw,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deterministic_remap_and_unchanged_types() -> None:
    graph = FakeGraph(
        [
            {"eid": "1", "name": "Python", "entity_type": "Technology"},
            {"eid": "2", "name": "cosmology", "entity_type": "Topic"},
            {"eid": "3", "name": "Ada Lovelace", "entity_type": "Person"},
            {"eid": "4", "name": "the Big Bang", "entity_type": "Event"},
        ]
    )
    report = await _run(graph, _classifier({}))

    types = {n["name"]: n["entity_type"] for n in graph.nodes}
    assert types == {
        "Python": "TechnicalArtifact",
        "cosmology": "DomainOrTopic",
        "Ada Lovelace": "Person",
        "the Big Bang": "Event",
    }
    # Only the two changed nodes carry the migration marker.
    marked = {n["name"] for n in graph.nodes if "entity_type_migration" in n}
    assert marked == {"Python", "cosmology"}
    assert report.deterministic == {"Technology->TechnicalArtifact": 1, "Topic->DomainOrTopic": 1}
    assert report.v1_remnants_after == {}
    assert report.success is True


@pytest.mark.asyncio
async def test_concept_happy_path_classifies_into_conceptual_family() -> None:
    graph = FakeGraph(
        [
            {
                "eid": "1",
                "name": "trie",
                "entity_type": "Concept",
                "description": "a data structure",
            },
            {
                "eid": "2",
                "name": "wavelength",
                "entity_type": "Concept",
                "description": "a property",
            },
            {
                "eid": "3",
                "name": "the GoLLIE paper",
                "entity_type": "Concept",
                "description": "a paper",
            },
        ]
    )
    report = await _run(
        graph,
        _classifier(
            {
                "trie": "MethodOrConcept",
                "wavelength": "QuantityMeasure",
                "the GoLLIE paper": "KnowledgeArtifact",
            }
        ),
    )
    types = {n["name"]: n["entity_type"] for n in graph.nodes}
    assert types == {
        "trie": "MethodOrConcept",
        "wavelength": "QuantityMeasure",
        "the GoLLIE paper": "KnowledgeArtifact",
    }
    assert report.concept_total == 3
    assert report.concept_classified == {
        "MethodOrConcept": 1,
        "QuantityMeasure": 1,
        "KnowledgeArtifact": 1,
    }
    assert report.concept_unclassified == []
    assert report.success is True


@pytest.mark.asyncio
async def test_concept_fail_closed_leaves_concept_and_blocks_success() -> None:
    graph = FakeGraph(
        [
            {"eid": "1", "name": "trie", "entity_type": "Concept", "description": ""},
            {"eid": "2", "name": "spacetime", "entity_type": "Concept", "description": ""},
        ]
    )
    # Classifier can type 'trie' but not 'spacetime' (returns None → fail-closed).
    report = await _run(graph, _classifier({"trie": "MethodOrConcept"}))

    by_name = {n["name"]: n for n in graph.nodes}
    assert by_name["trie"]["entity_type"] == "MethodOrConcept"
    assert by_name["spacetime"]["entity_type"] == "Concept"  # left, never guessed
    assert by_name["spacetime"]["entity_type_migration_error"] == "out_of_set"
    assert [u["name"] for u in report.concept_unclassified] == ["spacetime"]
    # A remaining Concept means AC-4 is not met → success is False.
    assert report.v1_remnants_after == {"Concept": 1}
    assert report.success is False


@pytest.mark.asyncio
async def test_rerun_is_idempotent_noop() -> None:
    graph = FakeGraph(
        [
            {"eid": "1", "name": "Python", "entity_type": "Technology"},
            {"eid": "2", "name": "trie", "entity_type": "Concept", "description": ""},
        ]
    )
    classifier = _classifier({"trie": "MethodOrConcept"})
    first = await _run(graph, classifier)
    assert first.success is True
    writes_after_first = graph.writes

    second = await _run(graph, classifier)
    # Nothing left to change on the second pass.
    assert graph.writes == writes_after_first
    assert second.deterministic == {"Technology->TechnicalArtifact": 0, "Topic->DomainOrTopic": 0}
    assert second.concept_total == 0
    assert second.success is True


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_but_previews() -> None:
    graph = FakeGraph(
        [
            {"eid": "1", "name": "Python", "entity_type": "Technology"},
            {"eid": "2", "name": "trie", "entity_type": "Concept", "description": ""},
        ]
    )
    before = await graph.count_by_type()
    report = await _run(graph, _classifier({"trie": "MethodOrConcept"}), dry_run=True)

    assert graph.writes == 0  # the hard dry-run contract
    assert await graph.count_by_type() == before  # graph unchanged
    # Still previews the work:
    assert report.deterministic == {"Technology->TechnicalArtifact": 1, "Topic->DomainOrTopic": 0}
    assert report.concept_total == 1
    assert report.concept_classified == {"MethodOrConcept": 1}
    assert report.success is False  # a dry run is never a completed migration


@pytest.mark.asyncio
async def test_class_property_is_never_touched() -> None:
    graph = FakeGraph(
        [{"eid": "1", "name": "Python", "entity_type": "Technology", "class": "World"}]
    )
    await _run(graph, _classifier({}))
    assert graph.nodes[0]["class"] == "World"


@pytest.mark.asyncio
async def test_concept_paging_terminates_past_fail_closed_node() -> None:
    # batch_size=1 with a fail-closed node in the middle must still terminate (cursor advances past it).
    graph = FakeGraph(
        [
            {"eid": "a", "name": "trie", "entity_type": "Concept", "description": ""},
            {"eid": "b", "name": "spacetime", "entity_type": "Concept", "description": ""},
            {"eid": "c", "name": "game theory", "entity_type": "Concept", "description": ""},
        ]
    )
    report = await _run(
        graph,
        _classifier({"trie": "MethodOrConcept", "game theory": "DomainOrTopic"}),
        batch_size=1,
    )
    assert report.concept_total == 3  # each visited exactly once
    assert len(report.concept_unclassified) == 1
    assert report.v1_remnants_after == {"Concept": 1}  # only 'spacetime' left


@pytest.mark.asyncio
async def test_rollback_restores_types_and_strips_markers(tmp_path) -> None:
    snap = tmp_path / "snap.json"
    graph = FakeGraph(
        [
            {"eid": "1", "name": "Python", "entity_type": "Technology"},
            {"eid": "2", "name": "trie", "entity_type": "Concept", "description": ""},
        ]
    )
    await _run(graph, _classifier({"trie": "MethodOrConcept"}), snapshot_path=snap)
    assert {n["name"]: n["entity_type"] for n in graph.nodes} == {
        "Python": "TechnicalArtifact",
        "trie": "MethodOrConcept",
    }

    restored = await run_rollback(graph, snap, batch_size=100)
    assert restored == 2
    assert {n["name"]: n["entity_type"] for n in graph.nodes} == {
        "Python": "Technology",
        "trie": "Concept",
    }
    assert all("entity_type_migration" not in n for n in graph.nodes)


def test_parse_classification_requires_unambiguous_single_hit() -> None:
    assert _parse_classification("MethodOrConcept") == "MethodOrConcept"
    assert _parse_classification("  QuantityMeasure\n") == "QuantityMeasure"
    assert _parse_classification("I think DomainOrTopic fits best") == "DomainOrTopic"
    assert _parse_classification("Person") is None  # not a conceptual target
    assert _parse_classification("could be MethodOrConcept or DomainOrTopic") is None  # ambiguous
    assert _parse_classification("") is None


def test_preflight_gate_logic_v1_refuses_v2_opens() -> None:
    # Robust to FRE-793 merge order: test the gate LOGIC against controlled maps, not the live module
    # (whose values flip from V1 to V2 the moment FRE-793 lands).
    v1_map = {"tool": "Technology", "topic": "Topic", "concept": "Concept", "person": "Person"}
    v2_map = {
        "tool": "TechnicalArtifact",
        "topic": "DomainOrTopic",
        "concept": ("MethodOrConcept", "DomainOrTopic", "Phenomenon"),
        "person": "Person",
    }
    assert _map_speaks_v2(v1_map) is False  # a retired V1 string present → gate refuses
    assert _map_speaks_v2(v2_map) is True  # V2-clean (incl. tuple values) → gate opens
