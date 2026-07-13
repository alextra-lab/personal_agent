"""Integration test for FRE-632 owner-identity unify against a real Neo4j (:7688).

Exercises the REAL code paths end to end:

  * the forward-fix — ``bootstrap_owner_identity`` labels the owner ``:Person:Entity``, and a
    subsequent ``create_entity`` with the owner's name resolves ONTO the owner node instead of
    forking a bare ``:Entity`` (the re-fork guard, ticket ask #3);
  * a non-owner name still creates a normal bare ``:Entity`` (no over-capture);
  * the migration Cypher — ``_Neo4jGraph.merge_one`` (``SET += apoc.map.removeKeys`` +
    ``apoc.refactor.mergeNodes``) folds a seeded split losslessly: identity props kept, entity
    props (embedding) transferred, parallel ``USES`` de-duped, ``DISCUSSES`` redirected, no
    self-loops; and ``find_split_entity_ids`` reports nothing left afterwards (idempotent).

Marked ``integration`` → skipped by ``make test``; run with the isolated test stack up
(``make test-infra-up``). All fixtures are uniquely-named so pre-existing test data is untouched.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from scripts.migrate_fre632_unify_owner_identity import _Neo4jGraph

from personal_agent.memory.models import Entity
from personal_agent.memory.service import MemoryService

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def service():
    """Connect to the test Neo4j; skip if unavailable."""
    svc = MemoryService()  # fre-375-allow: isolated test stack :7688
    if not await svc.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")
    yield svc
    await svc.disconnect()


async def _labels_and_count(driver, name: str) -> tuple[int, list[str], object]:
    async with driver.session() as session:
        # coalesce(is_owner, false): Neo4j collect() drops nulls, which would misalign the
        # positional owners list against label_sets for a bare (is_owner-null) entity.
        result = await session.run(
            "MATCH (n {name: $name}) "
            "RETURN count(n) AS c, collect(labels(n)) AS label_sets, "
            "       collect(coalesce(n.is_owner, false)) AS owners",
            name=name,
        )
        rec = await result.single()
    return int(rec["c"]), list(rec["label_sets"]), rec["owners"]


async def _cleanup(driver, *names: str) -> None:
    async with driver.session() as session:
        for name in names:
            await session.run("MATCH (n {name: $name}) DETACH DELETE n", name=name)
        await session.run("MATCH (a:Agent) WHERE a.id STARTS WITH 'fre632it-' DETACH DELETE a")


@pytest.mark.asyncio
async def test_owner_named_extraction_does_not_refork(service) -> None:
    """AC1 + AC3: after bootstrap, an extraction of the owner's own name lands on the owner node."""
    name = f"AlexFRE632IT-{uuid4().hex[:8]}"
    uid = uuid4()
    agent_id = f"fre632it-{uuid4().hex[:8]}"
    try:
        assert await service.bootstrap_owner_identity(agent_id, uid, "a@x.com", name) is True

        # The owner node is labelled :Person:Entity by the bootstrap fix.
        count, label_sets, owners = await _labels_and_count(service.driver, name)
        assert count == 1
        assert set(label_sets[0]) == {"Person", "Entity"}

        # Simulate extraction of the owner's own name — MUST resolve onto the owner node.
        await service.create_entity(Entity(name=name, entity_type="Person"))

        count, label_sets, owners = await _labels_and_count(service.driver, name)
        assert count == 1, "owner-named extraction must NOT fork a second node"
        assert set(label_sets[0]) == {"Person", "Entity"}
        assert owners[0] is True
    finally:
        await _cleanup(service.driver, name)


@pytest.mark.asyncio
async def test_non_owner_name_creates_bare_entity(service) -> None:
    """AC4: a third-party name is not over-captured — it becomes a normal bare :Entity."""
    name = f"BobFRE632IT-{uuid4().hex[:8]}"
    try:
        await service.create_entity(Entity(name=name, entity_type="Person"))
        count, label_sets, owners = await _labels_and_count(service.driver, name)
        assert count == 1
        assert set(label_sets[0]) == {"Entity"}  # no :Person, no owner
        assert owners[0] is False  # is_owner null → coalesced to false
    finally:
        await _cleanup(service.driver, name)


@pytest.mark.asyncio
async def test_migration_merge_is_lossless_and_idempotent(service) -> None:
    """AC2: merge_one folds the split losslessly — labels unioned, embedding transferred, parallel
    USES de-duped, DISCUSSES redirected, no self-loops; nothing left to merge afterwards.
    """
    name = f"SplitFRE632IT-{uuid4().hex[:8]}"
    uid = f"uid-{uuid4().hex[:8]}"
    graph = _Neo4jGraph(service.driver)
    try:
        # Seed the split: owner :Person (keyed by user_id, no embedding) + name-keyed :Entity with
        # a duplicated USES edge, a distinct RELATED_TO, and an inbound DISCUSSES from a Turn.
        async with service.driver.session() as s:
            await s.run(
                """
                CREATE (keep:Person {name:$name, user_id:$uid, is_owner:true, email:'a@x.com',
                                     source:'config_bootstrap'})
                CREATE (drop:Entity {name:$name, entity_type:'Person', description:'dev',
                                     embedding:[0.1,0.2,0.3], mention_count:112})
                CREATE (py:Entity {name:$py})
                CREATE (keep)-[:USES]->(py)
                CREATE (drop)-[:USES]->(py)
                CREATE (fa:Entity {name:$fa})
                CREATE (drop)-[:RELATED_TO]->(fa)
                CREATE (t:Turn {turn_id:$tid})
                CREATE (t)-[:DISCUSSES]->(drop)
                """,
                name=name,
                uid=uid,
                py=f"PyFRE632IT-{uuid4().hex[:6]}",
                fa=f"FaFRE632IT-{uuid4().hex[:6]}",
                tid=f"tFRE632IT-{uuid4().hex[:6]}",
            )

        # Resolve owner + split via the real seam Cypher.
        async with service.driver.session() as s:
            rec = await (
                await s.run(
                    "MATCH (o:Person {name:$name, is_owner:true}) RETURN elementId(o) AS eid",
                    name=name,
                )
            ).single()
        owner_eid = rec["eid"]
        drop_ids = await graph.find_split_entity_ids(name, owner_eid)
        assert len(drop_ids) == 1

        # Merge.
        merged_eid = await graph.merge_one(owner_eid, drop_ids[0])
        assert merged_eid == owner_eid  # keep's identity retained

        snap = await graph.snapshot(merged_eid)
        assert snap is not None
        assert set(snap.labels) == {"Person", "Entity"}
        assert snap.is_owner is True
        assert snap.has_embedding is True  # transferred from the split :Entity
        assert snap.rel_counts.get("OUT:USES") == 1  # de-duped 2 -> 1
        assert snap.rel_counts.get("IN:DISCUSSES") == 1  # redirected from the Turn
        assert snap.rel_counts.get("OUT:RELATED_TO") == 1  # moved from the split node

        # No self-loops introduced.
        async with service.driver.session() as s:
            rec = await (
                await s.run(
                    "MATCH (n)-[r]->(n) WHERE elementId(n)=$eid RETURN count(r) AS c",
                    eid=merged_eid,
                )
            ).single()
        assert rec["c"] == 0

        # Idempotent: only one node named, and nothing left to merge.
        count, _, _ = await _labels_and_count(service.driver, name)
        assert count == 1
        assert await graph.find_split_entity_ids(name, owner_eid) == []
    finally:
        # Clean up the seeded fixtures (owner, py/fa entities, turn).
        async with service.driver.session() as s:
            await s.run(
                "MATCH (n) WHERE n.name STARTS WITH 'SplitFRE632IT-' OR "
                "n.name STARTS WITH 'PyFRE632IT-' OR n.name STARTS WITH 'FaFRE632IT-' "
                "OR n.turn_id STARTS WITH 'tFRE632IT-' DETACH DELETE n"
            )
