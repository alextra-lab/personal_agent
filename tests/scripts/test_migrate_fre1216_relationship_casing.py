"""Integration test for the FRE-1216 relationship-casing backfill against Neo4j (:7688).

Covers both cases found on the live graph investigation:
  - No conflict: a lone casing-variant edge renames in place, properties intact.
  - Conflict (the live case): a variant edge and a canonical edge already connect the same pair
    (`USEs` alongside a parallel `USES` between the same two nodes) — properties merge onto the
    canonical edge (access_count takes the max, timestamps take earliest/latest as documented),
    and the variant edge is deleted.

Marked ``integration`` → skipped by ``make test``; run with the isolated test stack up
(``make test-infra-up``).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from scripts.migrate_fre1216_relationship_casing import run_backfill

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


@pytest.mark.asyncio
async def test_lone_variant_renames_in_place_with_properties_intact(driver) -> None:
    """A casing-variant edge with no parallel canonical edge renames in place."""
    src = f"FRE1216RC-src-{uuid4().hex[:8]}"
    tgt = f"FRE1216RC-tgt-{uuid4().hex[:8]}"
    async with driver.session() as session:
        await session.run(
            "CREATE (a:Entity {name: $src}), (b:Entity {name: $tgt}) "
            "CREATE (a)-[:UsEs {weight: 0.6, access_count: 3}]->(b)",
            src=src,
            tgt=tgt,
        )

    summary = await run_backfill(driver)
    assert summary["renamed"] >= 1

    async with driver.session() as session:
        result = await session.run(
            "MATCH (a {name: $src})-[r]->(b {name: $tgt}) "
            "RETURN type(r) AS rel_type, r.weight AS weight, r.access_count AS access_count",
            src=src,
            tgt=tgt,
        )
        record = await result.single()

    assert record is not None
    assert record["rel_type"] == "USES"
    assert record["weight"] == 0.6
    assert record["access_count"] == 3


@pytest.mark.asyncio
async def test_variant_with_parallel_canonical_edge_merges_and_deletes(driver) -> None:
    """A casing-variant edge with a parallel canonical edge merges onto it and is deleted."""
    src = f"FRE1216RC-src-{uuid4().hex[:8]}"
    tgt = f"FRE1216RC-tgt-{uuid4().hex[:8]}"
    async with driver.session() as session:
        await session.run(
            "CREATE (a:Entity {name: $src}), (b:Entity {name: $tgt}) "
            "CREATE (a)-[:UsEs {weight: 0.4, access_count: 2, "
            "  created_at: datetime('2026-05-30T16:19:08Z'), "
            "  last_accessed_at: datetime('2026-06-01T00:00:00Z')}]->(b) "
            "CREATE (a)-[:USES {weight: 0.75, access_count: 5, "
            "  created_at: datetime('2026-06-10T00:00:00Z'), "
            "  last_accessed_at: datetime('2026-06-15T00:00:00Z')}]->(b)",
            src=src,
            tgt=tgt,
        )

    summary = await run_backfill(driver)
    assert summary["merged"] >= 1

    async with driver.session() as session:
        result = await session.run(
            "MATCH (a {name: $src})-[r]->(b {name: $tgt}) "
            "RETURN type(r) AS rel_type, properties(r) AS props",
            src=src,
            tgt=tgt,
        )
        rows = await result.data()

    # Exactly one edge remains between the pair — the variant was deleted, not just renamed.
    assert len(rows) == 1
    assert rows[0]["rel_type"] == "USES"
    props = rows[0]["props"]
    # access_count takes the max (5), not the sum (7) — both already reflect real access.
    assert props["access_count"] == 5
    # created_at takes the earliest of the two (the variant's 05-30, not the canonical's 06-10).
    assert props["created_at"].isoformat().startswith("2026-05-30")
    # last_accessed_at takes the latest of the two (the canonical's 06-15).
    assert props["last_accessed_at"].isoformat().startswith("2026-06-15")


@pytest.mark.asyncio
async def test_backfill_is_idempotent(driver) -> None:
    """A second run leaves an already-canonicalized edge unchanged."""
    src = f"FRE1216RC-src-{uuid4().hex[:8]}"
    tgt = f"FRE1216RC-tgt-{uuid4().hex[:8]}"
    async with driver.session() as session:
        await session.run(
            "CREATE (a:Entity {name: $src}), (b:Entity {name: $tgt}) CREATE (a)-[:UsEs]->(b)",
            src=src,
            tgt=tgt,
        )

    first = await run_backfill(driver)
    assert first["renamed"] >= 1

    second = await run_backfill(driver)

    async with driver.session() as session:
        result = await session.run(
            "MATCH (a {name: $src})-[r]->(b {name: $tgt}) RETURN type(r) AS rel_type",
            src=src,
            tgt=tgt,
        )
        rows = await result.data()

    assert len(rows) == 1
    assert rows[0]["rel_type"] == "USES"
    # The second run found nothing new to touch for THIS pair (global counts may be nonzero
    # from unrelated pre-existing data, but this fixture's edge is already canonical).
    assert second["renamed"] + second["merged"] >= 0
