"""Integration test for the FRE-865 entity-class backfill against a real Neo4j (:7688).

Exercises the real :class:`_Neo4jGraph` Cypher end to end: seeds a fixture corpus of uniquely-named
``:Entity`` nodes with ``class IS NULL`` (a Personal fixture, a World fixture, a System-natured
fixture, and a fixture whose classifier response is deliberately unparseable), runs the backfill
with a deterministic fake classifier, and asserts: the right class lands on the right fixture, the
System-natured fixture is marked-for-dispatch (not classed), the fail-open fixture gets
``class=World`` with the fail-open marker, a second run is a no-op (idempotent, zero model calls),
and rollback restores the pre-run state — except a node whose ``last_seen`` was bumped after the
backfill wrote it, which rollback skips rather than clobbers.

Marked ``integration`` → skipped by ``make test``; run with the isolated test stack up
(``make test-infra-up``). Assertions are scoped to the seeded name prefix so pre-existing test data
neither fails this test nor is depended upon.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from scripts.migrate_fre865_entity_class_backfill import (
    BatchClassifyResult,
    ClassifyResult,
    EntityCandidate,
    _Neo4jGraph,
    run_backfill,
    run_rollback,
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
async def test_backfill_classifies_fixture_corpus_end_to_end(driver) -> None:
    prefix = f"FRE865IT-{uuid4().hex[:8]}"

    def n(suffix: str) -> str:
        return f"{prefix}-{suffix}"

    seed = [
        (n("cardiologist"), "Person", "Dr. Chen is my cardiologist"),
        (n("neo4j"), "TechnicalArtifact", "A graph database management system"),
        (
            n("postgres"),
            "TechnicalArtifact",
            "the agent's own database, referenced in a healthcheck",
        ),
        (n("mystery"), "Unknown", "ambiguous content the classifier cannot resolve"),
    ]
    try:
        async with driver.session() as session:
            for name, etype, desc in seed:
                await session.run(
                    "MERGE (e:Entity {name: $name}) SET e.entity_type = $etype, e.description = $desc",
                    name=name,
                    etype=etype,
                    desc=desc,
                )

        # Deterministic fake classifier: cardiologist->Personal, neo4j->World, postgres->finding
        # (System-natured, marked not classed), mystery is unmapped -> fail-open to World.
        mapping = {
            n("cardiologist"): ClassifyResult(
                output_kind="knowledge", knowledge_class="Personal", fail_open=False
            ),
            n("neo4j"): ClassifyResult(
                output_kind="knowledge", knowledge_class="World", fail_open=False
            ),
            n("postgres"): ClassifyResult(
                output_kind="finding", knowledge_class=None, fail_open=False
            ),
        }

        async def classifier(nodes: Sequence[EntityCandidate]) -> BatchClassifyResult:
            results = [
                mapping.get(
                    node.name,
                    ClassifyResult(
                        output_kind="knowledge",
                        knowledge_class="World",
                        fail_open=True,
                        reason="out_of_set",
                    ),
                )
                for node in nodes
            ]
            return BatchClassifyResult(results=results)

        graph = _Neo4jGraph(driver)
        run_id = f"fre865-it-{uuid4()}"
        now = datetime.now(timezone.utc).isoformat()

        report = await run_backfill(
            graph,
            classifier,
            run_id=run_id,
            now=now,
            prompt_version="fre865-it-v1",
            classifier_model="fake",
            batch_size=2,
        )

        cardiologist = await _props(driver, n("cardiologist"))
        assert cardiologist["class"] == "Personal"
        assert cardiologist["class_backfill_run_id"] == run_id

        neo4j = await _props(driver, n("neo4j"))
        assert neo4j["class"] == "World"

        postgres = await _props(driver, n("postgres"))
        assert "class" not in postgres  # never classed
        assert postgres["class_backfill_output_kind"] == "finding"

        mystery = await _props(driver, n("mystery"))
        assert mystery["class"] == "World"
        assert mystery["class_backfill_fail_open"] is True

        assert report.classified_personal >= 1
        assert report.classified_world >= 1
        assert report.marked_for_dispatch.get("finding", 0) >= 1
        assert report.fail_open_count >= 1

        # --- idempotent re-run: zero additional writes/model calls ---
        async def failing_classifier(nodes: Sequence[EntityCandidate]) -> BatchClassifyResult:
            raise AssertionError("classifier should not be called — no candidates remain")

        second = await run_backfill(
            graph,
            failing_classifier,
            run_id=run_id,
            now=now,
            prompt_version="fre865-it-v1",
            classifier_model="fake",
            batch_size=2,
        )
        assert second.total_candidates_this_run == 0
        assert second.model_calls == 0

        # --- rollback restores the pre-run state ---
        # NOTE: the shared test substrate may carry other pre-existing class=NULL entities from
        # other sessions' fixtures; fetch_candidates is intentionally unscoped (matches prod
        # behaviour — a real backfill must see the whole corpus), so this run's run_id may also
        # cover those. Assertions here are scoped to OUR named fixtures; `restored` is only
        # asserted as a lower bound.
        restored, skipped = await run_rollback(graph, run_id)
        assert restored >= 4
        assert skipped == []
        for name, *_ in seed:
            props = await _props(driver, name)
            assert "class" not in props
            assert "class_backfill_run_id" not in props
            assert "class_backfill_output_kind" not in props
            assert "class_backfill_fail_open" not in props
    finally:
        async with driver.session() as session:
            await session.run(
                "MATCH (e:Entity) WHERE e.name STARTS WITH $prefix DETACH DELETE e", prefix=prefix
            )


@pytest.mark.asyncio
async def test_rollback_skips_node_mutated_since_the_backfill(driver) -> None:
    prefix = f"FRE865IT-{uuid4().hex[:8]}"
    name = f"{prefix}-touched"
    try:
        async with driver.session() as session:
            await session.run(
                "MERGE (e:Entity {name: $name}) SET e.entity_type = 'Unknown', e.description = ''",
                name=name,
            )

        async def classifier(nodes: Sequence[EntityCandidate]) -> BatchClassifyResult:
            return BatchClassifyResult(
                results=[
                    ClassifyResult(
                        output_kind="knowledge", knowledge_class="World", fail_open=False
                    )
                    for _ in nodes
                ]
            )

        graph = _Neo4jGraph(driver)
        run_id = f"fre865-it-touched-{uuid4()}"
        now = datetime.now(timezone.utc).isoformat()
        await run_backfill(
            graph, classifier, run_id=run_id, now=now, prompt_version="v1", classifier_model="fake"
        )
        assert (await _props(driver, name))["class"] == "World"

        # Simulate live extraction touching this node after the backfill wrote it.
        async with driver.session() as session:
            await session.run(
                "MATCH (e:Entity {name: $name}) SET e.last_seen = datetime()", name=name
            )

        # NOTE: fetch_candidates is unscoped (matches prod — see the sibling test's comment), so
        # this run may also cover other pre-existing class=NULL entities in the shared test
        # substrate; only our named fixture is asserted.
        restored, skipped = await run_rollback(graph, run_id)
        assert name in skipped
        # left untouched, not clobbered
        assert (await _props(driver, name))["class"] == "World"
    finally:
        async with driver.session() as session:
            await session.run("MATCH (e:Entity {name: $name}) DETACH DELETE e", name=name)


@pytest.mark.asyncio
async def test_rollback_handles_last_seen_stored_as_plain_iso_string(driver) -> None:
    """last_seen is heterogeneous across the substrate (memory/service.py:278) — a plain ISO
    STRING on the Turn-DISCUSSES-Entity mention path, not always a native Neo4j datetime(). The
    rollback guard must still compare correctly (via toString()) rather than silently no-op'ing
    on a raw-type-vs-datetime Cypher comparison that evaluates to null on both branches.
    """
    prefix = f"FRE865IT-{uuid4().hex[:8]}"
    name = f"{prefix}-stringseen"
    try:
        async with driver.session() as session:
            await session.run(
                "MERGE (e:Entity {name: $name}) SET e.entity_type = 'Unknown', e.description = ''",
                name=name,
            )

        async def classifier(nodes: Sequence[EntityCandidate]) -> BatchClassifyResult:
            return BatchClassifyResult(
                results=[
                    ClassifyResult(
                        output_kind="knowledge", knowledge_class="World", fail_open=False
                    )
                    for _ in nodes
                ]
            )

        graph = _Neo4jGraph(driver)
        run_id = f"fre865-it-stringseen-{uuid4()}"
        now = datetime.now(timezone.utc).isoformat()
        await run_backfill(
            graph, classifier, run_id=run_id, now=now, prompt_version="v1", classifier_model="fake"
        )
        assert (await _props(driver, name))["class"] == "World"

        # last_seen as a plain ISO STRING (the mention-path shape), set BEFORE the backfill's
        # own class_backfill_at — i.e. NOT touched since the backfill ran, so rollback should
        # restore it (not skip it), proving the string-typed comparison isn't silently null.
        async with driver.session() as session:
            await session.run(
                "MATCH (e:Entity {name: $name}) SET e.last_seen = '2020-01-01T00:00:00+00:00'",
                name=name,
            )

        restored, skipped = await run_rollback(graph, run_id)
        assert name not in skipped
        props = await _props(driver, name)
        assert "class" not in props  # restored, not silently left in place
    finally:
        async with driver.session() as session:
            await session.run("MATCH (e:Entity {name: $name}) DETACH DELETE e", name=name)
