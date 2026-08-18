"""Integration test for the FRE-1216 temporal (first_seen/last_seen) backfill against Neo4j (:7688).

Seeds an :Entity fixture with STRING-typed first_seen/last_seen (reproducing the live-graph defect
directly, bypassing the application write path entirely), runs the backfill, and asserts both
fields are DATE_TIME afterward. Runs the backfill a second time to prove idempotency.

Marked ``integration`` → skipped by ``make test``; run with the isolated test stack up
(``make test-infra-up``).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from scripts.migrate_fre1216_temporal_backfill import run_backfill

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


async def _types(driver, name: str) -> dict:
    async with driver.session() as session:
        result = await session.run(
            "MATCH (e:Entity {name: $name}) "
            "RETURN apoc.meta.cypher.type(e.first_seen) AS fs_type, "
            "apoc.meta.cypher.type(e.last_seen) AS ls_type",
            name=name,
        )
        rec = await result.single()
    return dict(rec) if rec else {}


@pytest.mark.asyncio
async def test_backfill_converts_string_timestamps_to_datetime(driver) -> None:
    """STRING-typed first_seen/last_seen convert to native DATE_TIME."""
    name = f"FRE1216IT-{uuid4().hex[:8]}"
    async with driver.session() as session:
        await session.run(
            "CREATE (e:Entity {name: $name, first_seen: '2026-05-01T00:00:00+00:00', "
            "last_seen: '2026-05-02T00:00:00+00:00'})",
            name=name,
        )

    before = await _types(driver, name)
    assert before == {"fs_type": "STRING", "ls_type": "STRING"}

    counts = await run_backfill(driver)
    assert counts["first_seen"] >= 1
    assert counts["last_seen"] >= 1

    after = await _types(driver, name)
    assert after == {"fs_type": "DATE_TIME", "ls_type": "DATE_TIME"}


@pytest.mark.asyncio
async def test_backfill_is_idempotent(driver) -> None:
    """A second run leaves an already-converted node unchanged and does not error."""
    name = f"FRE1216IT-{uuid4().hex[:8]}"
    async with driver.session() as session:
        await session.run(
            "CREATE (e:Entity {name: $name, first_seen: '2026-05-01T00:00:00+00:00', "
            "last_seen: '2026-05-02T00:00:00+00:00'})",
            name=name,
        )

    await run_backfill(driver)
    first_pass = await _types(driver, name)

    # A second run must not error and must leave the already-converted node unchanged.
    await run_backfill(driver)
    second_pass = await _types(driver, name)

    assert first_pass == second_pass == {"fs_type": "DATE_TIME", "ls_type": "DATE_TIME"}


@pytest.mark.asyncio
async def test_backfill_does_not_touch_already_native_datetime(driver) -> None:
    """A node whose first_seen is already DATE_TIME is left byte-for-byte unchanged."""
    name = f"FRE1216IT-{uuid4().hex[:8]}"
    async with driver.session() as session:
        await session.run(
            "CREATE (e:Entity {name: $name, first_seen: datetime('2026-05-01T00:00:00Z'), "
            "last_seen: datetime('2026-05-02T00:00:00Z')})",
            name=name,
        )
        result = await session.run(
            "MATCH (e:Entity {name: $name}) RETURN e.first_seen AS fs", name=name
        )
        before_value = (await result.single())["fs"]

    await run_backfill(driver)

    async with driver.session() as session:
        result = await session.run(
            "MATCH (e:Entity {name: $name}) RETURN e.first_seen AS fs", name=name
        )
        after_value = (await result.single())["fs"]

    assert before_value == after_value
