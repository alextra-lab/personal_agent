"""Per-attempt telemetry writer tests (FRE-307).

Hits the live ``cloud-sim-postgres`` so the ORM round-trip + the
``(trace_id, attempt_number, role)`` unique constraint are exercised end
to end. ``attempt_number`` must be sequential per role within the same
trace_id; the helper derives it from ``MAX(attempt_number) + 1``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.config import settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn
from personal_agent.second_brain.attempts import (
    previous_attempt_count,
    record_consolidation_attempt,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def cleanup_pool() -> AsyncIterator[asyncpg.Pool]:
    """Direct asyncpg pool for tests that inspect ``consolidation_attempts`` rows."""
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.database_url), min_size=1, max_size=2
    )
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def trace_id() -> AsyncIterator[uuid4]:  # type: ignore[type-arg]
    """Unique trace_id per test; cleanup happens explicitly below."""
    yield uuid4()  # type: ignore[misc]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(trace_id) -> AsyncIterator[None]:  # noqa: ANN001
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.database_url), min_size=1, max_size=1
    )
    assert pool is not None
    try:
        yield
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM consolidation_attempts WHERE trace_id = $1", trace_id
            )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_first_attempt_is_numbered_one(trace_id, cleanup_pool: asyncpg.Pool) -> None:  # noqa: ANN001
    """First record for a (trace_id, role) gets attempt_number=1."""
    started = datetime.now(timezone.utc) - timedelta(seconds=5)
    n = await record_consolidation_attempt(
        trace_id=trace_id,
        role="entity_extraction",
        started_at=started,
        outcome="success",
    )
    assert n == 1

    async with cleanup_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT attempt_number, outcome, role, denial_reason "
            "FROM consolidation_attempts WHERE trace_id = $1",
            trace_id,
        )
    assert row is not None
    assert row["attempt_number"] == 1
    assert row["outcome"] == "success"
    assert row["role"] == "entity_extraction"
    assert row["denial_reason"] is None


@pytest.mark.asyncio
async def test_subsequent_attempts_increment(trace_id, cleanup_pool: asyncpg.Pool) -> None:  # noqa: ANN001
    """Successive records for the same (trace_id, role) increment by one."""
    started = datetime.now(timezone.utc)
    for expected in (1, 2, 3):
        n = await record_consolidation_attempt(
            trace_id=trace_id,
            role="entity_extraction",
            started_at=started,
            outcome="extraction_returned_fallback",
        )
        assert n == expected

    async with cleanup_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT attempt_number FROM consolidation_attempts "
            "WHERE trace_id = $1 ORDER BY attempt_number",
            trace_id,
        )
    assert [r["attempt_number"] for r in rows] == [1, 2, 3]


@pytest.mark.asyncio
async def test_distinct_roles_have_independent_counters(
    trace_id, cleanup_pool: asyncpg.Pool  # noqa: ANN001
) -> None:
    """Different roles for the same trace_id each start at 1."""
    started = datetime.now(timezone.utc)
    await record_consolidation_attempt(
        trace_id=trace_id, role="entity_extraction", started_at=started, outcome="success"
    )
    n = await record_consolidation_attempt(
        trace_id=trace_id, role="promotion", started_at=started, outcome="success"
    )
    assert n == 1, "promotion's first attempt should be numbered 1, not 2"


@pytest.mark.asyncio
async def test_budget_denied_records_denial_reason(trace_id) -> None:  # noqa: ANN001
    """budget_denied outcomes carry the structured denial_reason field."""
    started = datetime.now(timezone.utc)
    n = await record_consolidation_attempt(
        trace_id=trace_id,
        role="entity_extraction",
        started_at=started,
        outcome="budget_denied",
        denial_reason="cap_exceeded",
    )
    assert n == 1
    assert (
        await previous_attempt_count(trace_id=trace_id, role="entity_extraction")
    ) == 1
