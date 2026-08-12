"""FRE-1210 T6.1 -- write_kg_stats round-trip against the test Postgres stack.

Redirected to :5433 via ``tests/conftest.py`` (FRE-375). Skips cleanly if the
test stack isn't running.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.config import settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn
from personal_agent.memory.kg_stats_aggregate import KgStatRow, write_kg_stats


@pytest_asyncio.fixture
async def pg_conn():
    """Raw connection to the test Postgres, for setup/verification only."""
    dsn = _normalize_asyncpg_dsn(settings.database_url)
    try:
        conn = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"test-stack Postgres unavailable ({exc}); run `make test-infra-up`")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_write_kg_stats_round_trips(pg_conn: asyncpg.Connection) -> None:
    """Every row is written and readable back by its metric_name."""
    marker = f"fre1210-write-{uuid.uuid4().hex[:8]}"
    trace_id = f"trace-{marker}"
    rows = [
        KgStatRow(marker, None, 0.5),
        KgStatRow(marker, "Person", 3.0),
    ]

    written = await write_kg_stats(rows, trace_id)
    assert written == 2

    try:
        count = await pg_conn.fetchval(
            "SELECT count(*) FROM kg_stats WHERE metric_name = $1", marker
        )
        assert count == 2
    finally:
        await pg_conn.execute("DELETE FROM kg_stats WHERE metric_name = $1", marker)


@pytest.mark.asyncio
async def test_write_kg_stats_empty_rows_is_a_noop() -> None:
    """An empty row list writes nothing and doesn't open a connection."""
    written = await write_kg_stats([], "trace-empty")
    assert written == 0
