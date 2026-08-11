"""FRE-1210 AC-3 -- the cold-mass number can move.

End-to-end: seed a never-read entity in the live test Neo4j, run the full
aggregate_kg_stats + write_kg_stats pipeline against the live test Postgres,
simulate a real access, re-run, and assert cold_mass_ratio strictly
decreases. This is AC-3's literal wording as a test, not an inference from
the unit-level cold_mass_ratio tests passing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.config import settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn
from personal_agent.memory.kg_stats_aggregate import aggregate_kg_stats, write_kg_stats
from personal_agent.memory.service import MemoryService

_MARKER = "fre1210_ac3"


@pytest_asyncio.fixture
async def graph():
    """Connected MemoryService against the test Neo4j; removes its own data."""
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    if not await service.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")
    yield service
    if service.driver:
        async with service.driver.session() as session:
            await session.run(f"MATCH (n {{{_MARKER}: true}}) DETACH DELETE n")
    await service.disconnect()


@pytest_asyncio.fixture
async def pg_conn():
    """Raw connection to the test Postgres, for reading back written rows."""
    dsn = _normalize_asyncpg_dsn(settings.database_url)
    try:
        conn = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"test-stack Postgres unavailable ({exc}); run `make test-infra-up`")
    try:
        yield conn
    finally:
        await conn.close()


async def _latest_cold_mass_ratio(conn: asyncpg.Connection, window_start: datetime) -> float:
    value = await conn.fetchval(
        "SELECT metric_value FROM kg_stats WHERE metric_name = 'cold_mass_ratio' "
        "AND observed_at >= $1 ORDER BY observed_at DESC LIMIT 1",
        window_start,
    )
    assert value is not None, "cold_mass_ratio row not written in this test's window"
    return float(value)


@pytest.mark.asyncio
async def test_accessing_cold_entity_decrements_never_read_count(
    graph: MemoryService, pg_conn: asyncpg.Connection
) -> None:
    """AC-3: accessing a previously-cold entity lowers cold_mass_ratio on the next run."""
    tag = uuid.uuid4().hex[:8]
    name = f"ac3-cold-{tag}"
    async with graph.driver.session() as session:  # type: ignore[union-attr]
        await session.run(
            f"CREATE (:Entity {{name: $name, entity_type: 'MethodOrConcept', "
            f"access_count: 0, last_accessed_at: datetime(), {_MARKER}: true}})",
            name=name,
        )

    window_start_1 = datetime.now(timezone.utc)
    rows_1 = await aggregate_kg_stats(graph.driver)  # type: ignore[arg-type]
    written_1 = await write_kg_stats(rows_1, trace_id=f"trace-{tag}-1")
    assert written_1 > 0
    ratio_before = await _latest_cold_mass_ratio(pg_conn, window_start_1)

    async with graph.driver.session() as session:  # type: ignore[union-attr]
        await session.run(
            "MATCH (e:Entity {name: $name}) SET e.access_count = 1, e.last_accessed_at = datetime()",
            name=name,
        )

    window_start_2 = datetime.now(timezone.utc)
    rows_2 = await aggregate_kg_stats(graph.driver)  # type: ignore[arg-type]
    written_2 = await write_kg_stats(rows_2, trace_id=f"trace-{tag}-2")
    assert written_2 > 0
    ratio_after = await _latest_cold_mass_ratio(pg_conn, window_start_2)

    try:
        assert ratio_after < ratio_before
    finally:
        await pg_conn.execute("DELETE FROM kg_stats WHERE observed_at >= $1", window_start_1)
