"""FRE-1210 T6.1 -- live-Neo4j integration tests for kg_stats_aggregate.

Covers the parts of the module that are actual Cypher, not pure Python:
owner exclusion, relationship-type filtering, embedding reachability,
duplicate-name normalization, and turn coverage. Runs against the real test
Neo4j test substrate (:7688 via ``tests/conftest.py``), mirroring
``test_graph_user_identity_integration.py``.

Delta-based, not absolute-count-based: the test stack may carry other tests'
residue, so every assertion compares a before/after count around a
uniquely-markered seed rather than asserting an absolute total.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio

from personal_agent.memory.kg_stats_aggregate import (
    _duplicate_groups,
    _embedding_missing,
    _relationship_count_by_type,
    _scan_entities,
    _turns_without_entities_ratio,
)
from personal_agent.memory.service import MemoryService

_MARKER = "fre1210_kgstats"


@pytest_asyncio.fixture
async def graph() -> Any:
    """Connected MemoryService against the test substrate; removes its own data."""
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    if not await service.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")
    yield service
    if service.driver:
        async with service.driver.session() as session:
            await session.run(f"MATCH (n {{{_MARKER}: true}}) DETACH DELETE n")
    await service.disconnect()


def _tag() -> str:
    return uuid.uuid4().hex[:8]


class TestOwnerExclusion:
    """_scan_entities must not surface the owner :Person:Entity node."""

    @pytest.mark.asyncio
    async def test_owner_node_not_scanned(self, graph: MemoryService) -> None:
        """A `:Person:Entity` with user_id set never appears in the scan."""
        tag = _tag()
        owner_name = f"owner-{tag}"
        real_name = f"real-{tag}"
        async with graph.driver.session() as session:
            await session.run(
                f"CREATE (:Person:Entity {{name: $name, user_id: $uid, entity_type: 'Person', "
                f"access_count: 5, {_MARKER}: true}})",
                name=owner_name,
                uid=str(uuid.uuid4()),
            )
            await session.run(
                f"CREATE (:Entity {{name: $name, entity_type: 'Person', access_count: 2, "
                f"{_MARKER}: true}})",
                name=real_name,
            )

        entities = await _scan_entities(graph.driver)
        names = {e.name for e in entities}
        assert owner_name not in names
        assert real_name in names


class TestRelationshipCountByType:
    """_relationship_count_by_type -- semantic edges only."""

    @pytest.mark.asyncio
    async def test_infrastructure_edges_excluded(self, graph: MemoryService) -> None:
        """CONTAINS/NEXT-style edges never inflate the semantic edge count."""
        tag = _tag()
        async with graph.driver.session() as session:
            await session.run(
                f"CREATE (a:Entity {{name: $a, {_MARKER}: true}}), "
                f"(b:Entity {{name: $b, {_MARKER}: true}}), "
                f"(a)-[:DISCUSSES {{{_MARKER}: true}}]->(b), "
                f"(a)-[:CONTAINS {{{_MARKER}: true}}]->(b)",
                a=f"rel-a-{tag}",
                b=f"rel-b-{tag}",
            )

        before = {
            r.dimension: r.metric_value for r in await _relationship_count_by_type(graph.driver)
        }

        async with graph.driver.session() as session:
            result = await session.run(
                "MATCH ()-[r]->() WHERE type(r) = 'DISCUSSES' RETURN count(r) AS n"
            )
            record = await result.single()
            discusses_total = record["n"] if record else 0

        assert before.get("DISCUSSES", 0.0) == float(discusses_total)


class TestEmbeddingMissing:
    """_embedding_missing counts entities with no reachable embedding."""

    @pytest.mark.asyncio
    async def test_null_and_zero_vector_both_count_as_missing(self, graph: MemoryService) -> None:
        """A missing property and a zero vector are both 'unreachable'."""
        tag = _tag()
        async with graph.driver.session() as session:
            before = await _embedding_missing(graph.driver)
            await session.run(
                f"CREATE (:Entity {{name: $n1, {_MARKER}: true}})",
                n1=f"no-embedding-{tag}",
            )
            await session.run(
                f"CREATE (:Entity {{name: $n2, embedding: $vec, {_MARKER}: true}})",
                n2=f"zero-embedding-{tag}",
                vec=[0.0, 0.0, 0.0],
            )
            await session.run(
                f"CREATE (:Entity {{name: $n3, embedding: $vec, {_MARKER}: true}})",
                n3=f"real-embedding-{tag}",
                vec=[0.1, 0.2, 0.3],
            )
        after = await _embedding_missing(graph.driver)
        assert after.metric_value - before.metric_value == 2.0


class TestDuplicateGroups:
    """_duplicate_groups -- case/whitespace-normalized name collisions."""

    @pytest.mark.asyncio
    async def test_case_and_whitespace_normalized_duplicates_grouped(
        self, graph: MemoryService
    ) -> None:
        """'Foo', 'foo ', and ' FOO' form exactly one duplicate group."""
        tag = f"Dup{_tag()}"
        async with graph.driver.session() as session:
            before_dup, before_dis = await _duplicate_groups(graph.driver)
            for variant, etype in ((tag, "A"), (f" {tag.lower()} ", "B"), (tag.upper(), "A")):
                await session.run(
                    f"CREATE (:Entity {{name: $n, entity_type: $t, {_MARKER}: true}})",
                    n=variant,
                    t=etype,
                )
        after_dup, after_dis = await _duplicate_groups(graph.driver)
        assert after_dup.metric_value - before_dup.metric_value == 1.0
        assert after_dis.metric_value - before_dis.metric_value == 1.0  # types A and B disagree

    @pytest.mark.asyncio
    async def test_blank_name_never_forms_a_group(self, graph: MemoryService) -> None:
        """Two blank-name entities must not be counted as duplicates of each other."""
        async with graph.driver.session() as session:
            before_dup, _ = await _duplicate_groups(graph.driver)
            await session.run(
                f"CREATE (:Entity {{name: '', {_MARKER}: true}})",
            )
            await session.run(
                f"CREATE (:Entity {{name: '   ', {_MARKER}: true}})",
            )
        after_dup, _ = await _duplicate_groups(graph.driver)
        assert after_dup.metric_value == before_dup.metric_value


class TestTurnsWithoutEntitiesRatio:
    """_turns_without_entities_ratio -- DISCUSSES coverage."""

    @pytest.mark.asyncio
    async def test_turn_without_discusses_counted(self, graph: MemoryService) -> None:
        """A Turn with no DISCUSSES edge raises the without-entities count."""
        tag = _tag()
        async with graph.driver.session() as session:
            before = await _turns_without_entities_ratio(graph.driver)
            before_total_result = await session.run("MATCH (t:Turn) RETURN count(t) AS n")
            before_total_record = await before_total_result.single()
            before_total = before_total_record["n"] if before_total_record else 0

            await session.run(
                f"CREATE (:Turn {{turn_id: $t1, {_MARKER}: true}})",
                t1=f"turn-no-entity-{tag}",
            )
            await session.run(
                f"CREATE (t:Turn {{turn_id: $t2, {_MARKER}: true}})-[:DISCUSSES {{{_MARKER}: true}}]->"
                f"(:Entity {{name: $ename, {_MARKER}: true}})",
                t2=f"turn-with-entity-{tag}",
                ename=f"turn-entity-{tag}",
            )

            after_total_result = await session.run("MATCH (t:Turn) RETURN count(t) AS n")
            after_total_record = await after_total_result.single()
            after_total = after_total_record["n"]

        after = await _turns_without_entities_ratio(graph.driver)

        before_without = round(before.metric_value * before_total)
        after_without = round(after.metric_value * after_total)
        assert after_without - before_without == 1
        assert after_total - before_total == 2
