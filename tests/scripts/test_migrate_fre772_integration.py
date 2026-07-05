"""Integration test for the FRE-772 migration against a real Neo4j (:7688).

Exercises the real :class:`_Neo4jGraph` Cypher end to end: seeds uniquely-named V1-typed ``:Entity``
nodes (+ a relationship + a ``class`` property + an ``Unknown`` + a ``Concept``), runs the migration
with a deterministic fake classifier, and asserts every seeded node is correctly re-typed with no V1
remnant, the ``class`` and ``originating_*`` provenance are untouched, the relationship survives (no
orphaning), and a re-run is a no-op that still retries a fail-closed node.

Marked ``integration`` → skipped by ``make test``; run with the isolated test stack up
(``make test-infra-up``). Assertions are scoped to the seeded name prefix so pre-existing test data
neither fails this test nor is depended upon.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from scripts.migrate_fre772_entity_type_v2 import (
    ClassifyResult,
    _Neo4jGraph,
    run_migration,
)

from personal_agent.memory.service import MemoryService

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def driver():
    """Connect to the test Neo4j; skip if unavailable."""
    service = MemoryService()  # fre-375-allow: isolated test stack :7688
    if not await service.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")
    yield service.driver
    await service.disconnect()


async def _props(driver, name: str) -> dict:
    async with driver.session() as session:
        result = await session.run(
            "MATCH (e:Entity {name: $name}) RETURN properties(e) AS p", name=name
        )
        rec = await result.single()
    return dict(rec["p"]) if rec else {}


@pytest.mark.asyncio
async def test_migration_retypes_seeded_nodes_end_to_end(driver) -> None:
    prefix = f"FRE772IT-{uuid4().hex[:8]}"

    def n(suffix: str) -> str:
        return f"{prefix}-{suffix}"

    # --- seed V1-typed nodes + a relationship, with class + origination provenance ---
    seed = [
        (n("python"), "Technology", "the language"),
        (n("cosmology"), "Topic", "the field"),
        (n("ada"), "Person", "a person"),
        (n("trie"), "Concept", "a prefix-tree data structure"),
        (n("spacetime"), "Concept", "the fabric"),
        (n("mystery"), "Unknown", "untyped legacy node"),
    ]
    try:
        async with driver.session() as session:
            for name, etype, desc in seed:
                await session.run(
                    "MERGE (e:Entity {name: $name}) "
                    "SET e.entity_type = $etype, e.description = $desc, "
                    "    e.class = 'World', "
                    "    e.originating_trace_id = 'seed-trace', "
                    "    e.originating_session_id = 'seed-session'",
                    name=name,
                    etype=etype,
                    desc=desc,
                )
            await session.run(
                "MATCH (a:Entity {name: $a}), (b:Entity {name: $b}) MERGE (a)-[:RELATED_TO]->(b)",
                a=n("python"),
                b=n("trie"),
            )

        # Classifier types 'trie' but not 'spacetime' (fail-closed).
        async def classifier(name: str, description: str) -> ClassifyResult:
            if name == n("trie"):
                return ClassifyResult(entity_type="MethodOrConcept")
            return ClassifyResult(entity_type=None, reason="out_of_set")

        graph = _Neo4jGraph(driver)
        run_id = f"fre772-it-{uuid4()}"
        now = datetime.now(timezone.utc).isoformat()

        # --- run the migration ---
        await run_migration(
            graph, classifier, run_id=run_id, now=now, classifier_model="fake", batch_size=2
        )

        # --- deterministic remaps + unchanged types ---
        assert (await _props(driver, n("python")))["entity_type"] == "TechnicalArtifact"
        assert (await _props(driver, n("cosmology")))["entity_type"] == "DomainOrTopic"
        assert (await _props(driver, n("ada")))["entity_type"] == "Person"
        assert (await _props(driver, n("mystery")))["entity_type"] == "Unknown"  # untouched

        # --- Concept re-classification: happy + fail-closed ---
        trie = await _props(driver, n("trie"))
        assert trie["entity_type"] == "MethodOrConcept"
        assert trie["entity_type_migration"] == run_id
        spacetime = await _props(driver, n("spacetime"))
        assert spacetime["entity_type"] == "Concept"  # left, never guessed
        assert spacetime["entity_type_migration_error"] == "out_of_set"

        # --- class + origination provenance untouched on a re-typed node ---
        py = await _props(driver, n("python"))
        assert py["class"] == "World"
        assert py["originating_trace_id"] == "seed-trace"
        assert py["originating_session_id"] == "seed-session"
        assert py["entity_type_migration"] == run_id

        # --- no V1 remnant among seeded nodes except the one fail-closed Concept ---
        async with driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity) WHERE e.name STARTS WITH $prefix "
                "AND e.entity_type IN ['Technology', 'Topic'] RETURN count(e) AS n",
                prefix=prefix,
            )
            assert (await result.single())["n"] == 0

        # --- relationship survived (no orphaning from a scalar re-type) ---
        async with driver.session() as session:
            result = await session.run(
                "MATCH (:Entity {name: $a})-[r:RELATED_TO]->(:Entity {name: $b}) RETURN count(r) AS n",
                a=n("python"),
                b=n("trie"),
            )
            assert (await result.single())["n"] == 1

        # --- idempotent re-run: deterministic no-op, fail-closed Concept retried ---
        # This time the classifier can type spacetime → it converts on the retry.
        async def classifier2(name: str, description: str) -> ClassifyResult:
            if name == n("spacetime"):
                return ClassifyResult(entity_type="Phenomenon")
            return ClassifyResult(entity_type=None, reason="out_of_set")

        await run_migration(
            graph, classifier2, run_id=run_id, now=now, classifier_model="fake", batch_size=2
        )
        # trie unchanged (already V2, not re-fetched); spacetime now resolved.
        assert (await _props(driver, n("trie")))["entity_type"] == "MethodOrConcept"
        assert (await _props(driver, n("spacetime")))["entity_type"] == "Phenomenon"

        # No seeded V1/Concept remnants at all now.
        async with driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity) WHERE e.name STARTS WITH $prefix "
                "AND e.entity_type IN ['Technology', 'Topic', 'Concept'] RETURN count(e) AS n",
                prefix=prefix,
            )
            assert (await result.single())["n"] == 0
    finally:
        async with driver.session() as session:
            await session.run(
                "MATCH (e:Entity) WHERE e.name STARTS WITH $prefix DETACH DELETE e", prefix=prefix
            )
