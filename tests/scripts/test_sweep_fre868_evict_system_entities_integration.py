"""Integration test for the FRE-868 System-entity eviction sweep against real test-substrate
Neo4j (:7688) + sysgraph Postgres (:5433).

Exercises the real :class:`_Neo4jGraph`/:class:`_SysgraphSink` Cypher/SQL end to end: seeds a
fixture corpus (an ``ephemeral``-marked entity, a ``finding``-marked entity, a ``knowledge``-classed
entity that must survive untouched, an Entity<->Entity edge between the two marked entities, a
Turn-DISCUSSES->Entity edge, and a datetime-valued property), runs the sweep, and asserts: marked
entities + their edges are gone from Core; the ``finding`` entity produced a queryable
``sysgraph.stat`` row; the ``knowledge`` entity is untouched; a second sweep is a no-op; rollback
from the written snapshot file recreates both evicted entities (including the datetime property),
reconnects both edges exactly once, and running rollback again is idempotent.

Marked ``integration`` -> skipped by ``make test``; run with the isolated test stack up
(``make test-infra-up``). Assertions are scoped to the seeded name prefix so pre-existing test data
neither fails this test nor is depended upon.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from scripts.sweep_fre868_evict_system_entities import (
    SnapshotWriter,
    _Neo4jGraph,
    _SysgraphSink,
    run_rollback,
    run_sweep,
)

from personal_agent.config import settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn
from personal_agent.memory.service import MemoryService
from personal_agent.sysgraph import SysgraphRepository

pytestmark = pytest.mark.integration


class _FakeSysgraphFailAlways:
    """Deterministic failing sysgraph sink for the dispatch-failure regression test."""

    async def record_finding(
        self, *, entity_name: str, entity_type: str, description: str | None
    ) -> None:
        raise RuntimeError("simulated sysgraph outage")


@pytest_asyncio.fixture
async def driver():
    """Connect to the test Neo4j; skip if unavailable."""
    service = MemoryService()  # fre-375-allow: isolated test stack :7688
    if not await service.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")
    yield service.driver
    await service.disconnect()


@pytest_asyncio.fixture
async def sysgraph_repo() -> AsyncIterator[SysgraphRepository]:
    repo = SysgraphRepository(dsn=settings.sysgraph_database_url)
    await repo.connect()
    try:
        yield repo
    finally:
        await repo.disconnect()


@pytest_asyncio.fixture
async def sysgraph_pool() -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.sysgraph_database_url),
        min_size=1,
        max_size=2,
        command_timeout=10,
    )
    try:
        yield pool
    finally:
        await pool.close()


async def _node_exists(driver, name: str) -> bool:
    async with driver.session() as session:
        result = await session.run("MATCH (e {name: $name}) RETURN count(e) AS n", name=name)
        rec = await result.single()
    return bool(rec and rec["n"] > 0)


async def _props(driver, name: str) -> dict:
    async with driver.session() as session:
        result = await session.run("MATCH (e {name: $name}) RETURN properties(e) AS p", name=name)
        rec = await result.single()
    return dict(rec["p"]) if rec else {}


async def _rel_count(driver, name_a: str, name_b: str, rel_type: str) -> int:
    async with driver.session() as session:
        result = await session.run(
            f"MATCH (a {{name: $a}})-[r:{rel_type}]-(b {{name: $b}}) RETURN count(r) AS n",
            a=name_a,
            b=name_b,
        )
        rec = await result.single()
    return int(rec["n"]) if rec else 0


@pytest.mark.asyncio
async def test_sweep_evicts_marked_entities_dispatches_finding_and_rollback_restores(
    driver, sysgraph_repo, sysgraph_pool, tmp_path: Path
) -> None:
    prefix = f"FRE868IT-{uuid4().hex[:8]}"

    def n(suffix: str) -> str:
        return f"{prefix}-{suffix}"

    turn_id = f"{prefix}-turn"
    ephemeral_name = n("ephemeral")
    finding_name = n("finding")
    knowledge_name = n("knowledge")
    dt_value = datetime(2026, 7, 12, 3, 4, 5, tzinfo=timezone.utc)

    try:
        async with driver.session() as session:
            await session.run(
                "MERGE (e:Entity {name: $name}) "
                "SET e.entity_type = 'Unknown', e.description = 'transient scaffold', "
                "    e.class_backfill_output_kind = 'ephemeral', e.class_backfill_at = datetime($dt)",
                name=ephemeral_name,
                dt=dt_value.isoformat(),
            )
            await session.run(
                "MERGE (e:Entity {name: $name}) "
                "SET e.entity_type = 'TechnicalArtifact', e.description = 'infra self-observation', "
                "    e.class_backfill_output_kind = 'finding'",
                name=finding_name,
            )
            await session.run(
                "MERGE (e:Entity {name: $name}) SET e.entity_type = 'Concept', e.class = 'World'",
                name=knowledge_name,
            )
            await session.run(
                "MATCH (a:Entity {name: $a}), (b:Entity {name: $b}) "
                "MERGE (a)-[:RELATED_TO {weight: 0.5}]->(b)",
                a=ephemeral_name,
                b=finding_name,
            )
            await session.run(
                "MERGE (t:Turn {turn_id: $turn_id}) "
                "WITH t MATCH (e:Entity {name: $name}) MERGE (t)-[:DISCUSSES]->(e)",
                turn_id=turn_id,
                name=ephemeral_name,
            )

        graph = _Neo4jGraph(driver)
        sysgraph = _SysgraphSink(sysgraph_repo)
        snapshot_path = tmp_path / "snap.jsonl"
        writer = SnapshotWriter(snapshot_path)
        run_id = f"fre868-it-{uuid4()}"
        now = datetime.now(timezone.utc).isoformat()

        try:
            report = await run_sweep(
                graph, sysgraph, writer, run_id=run_id, now=now, dry_run=False, batch_size=10
            )
        finally:
            writer.close()

        assert report.evicted_ephemeral >= 1
        assert report.evicted_finding >= 1

        # marked entities are gone from Core
        assert not await _node_exists(driver, ephemeral_name)
        assert not await _node_exists(driver, finding_name)
        assert await _rel_count(driver, ephemeral_name, finding_name, "RELATED_TO") == 0

        # knowledge entity untouched
        assert await _node_exists(driver, knowledge_name)

        # finding dispatched to sysgraph as a queryable stat row
        async with sysgraph_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT metadata FROM sysgraph.stat WHERE name = 'dispatch_finding_observed' "
                "AND metadata->>'entity_name' = $1 ORDER BY observed_at DESC LIMIT 1",
                finding_name,
            )
        assert row is not None
        metadata = json.loads(row["metadata"])
        assert metadata["entity_type"] == "TechnicalArtifact"

        # second sweep is a no-op
        writer2 = SnapshotWriter(tmp_path / "snap2.jsonl")
        try:
            second = await run_sweep(
                graph,
                sysgraph,
                writer2,
                run_id=f"{run_id}-2",
                now=now,
                dry_run=False,
                batch_size=10,
            )
        finally:
            writer2.close()
        assert second.total_candidates_this_run == 0

        # --- rollback ---
        restored_nodes, restored_rels, skipped = await run_rollback(graph, snapshot_path, run_id)
        assert restored_nodes >= 2
        assert restored_rels >= 2  # RELATED_TO (deduped) + DISCUSSES
        assert skipped == []

        restored_ephemeral_props = await _props(driver, ephemeral_name)
        assert restored_ephemeral_props != {}
        assert restored_ephemeral_props["class_backfill_at"] == dt_value

        assert await _rel_count(driver, ephemeral_name, finding_name, "RELATED_TO") == 1

        async with driver.session() as session:
            result = await session.run(
                "MATCH (t:Turn {turn_id: $turn_id})-[:DISCUSSES]->(e {name: $name}) "
                "RETURN count(*) AS n",
                turn_id=turn_id,
                name=ephemeral_name,
            )
            rec = await result.single()
        assert rec["n"] == 1

        # rollback is idempotent — same counts on a second call
        restored_nodes_2, restored_rels_2, skipped_2 = await run_rollback(
            graph, snapshot_path, run_id
        )
        assert restored_nodes_2 == restored_nodes
        assert restored_rels_2 == restored_rels
        assert skipped_2 == []
    finally:
        async with driver.session() as session:
            await session.run(
                "MATCH (e) WHERE e.name STARTS WITH $prefix OR e.turn_id = $turn_id "
                "DETACH DELETE e",
                prefix=prefix,
                turn_id=turn_id,
            )
        async with sysgraph_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sysgraph.stat WHERE name = 'dispatch_finding_observed' "
                "AND metadata->>'entity_name' = $1",
                finding_name,
            )


@pytest.mark.asyncio
async def test_finding_dispatch_failure_leaves_node_untouched_against_real_neo4j(
    driver, tmp_path: Path
) -> None:
    prefix = f"FRE868IT-{uuid4().hex[:8]}"
    name = f"{prefix}-flaky-finding"
    try:
        async with driver.session() as session:
            await session.run(
                "MERGE (e:Entity {name: $name}) "
                "SET e.entity_type = 'Unknown', e.class_backfill_output_kind = 'finding'",
                name=name,
            )

        graph = _Neo4jGraph(driver)
        writer = SnapshotWriter(tmp_path / "snap.jsonl")
        run_id = f"fre868-it-fail-{uuid4()}"
        now = datetime.now(timezone.utc).isoformat()

        try:
            report = await run_sweep(
                graph,
                _FakeSysgraphFailAlways(),
                writer,
                run_id=run_id,
                now=now,
                dry_run=False,
                batch_size=10,
            )
        finally:
            writer.close()

        assert report.dispatch_finding_failed >= 1
        assert await _node_exists(driver, name)
        props = await _props(driver, name)
        assert props["class_backfill_output_kind"] == "finding"  # marker intact, retried next run
    finally:
        async with driver.session() as session:
            await session.run("MATCH (e {name: $name}) DETACH DELETE e", name=name)
