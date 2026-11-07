"""Fixtures for the broker's integration tests (FRE-1683).

The ledger runs as the broker's own restricted role, ``seshat_dev_broker``, so a test that passes
also proves that the role's grants are enough. Tokens are written as ``agent``, the way the gateway
writes them. Both connect to the test stack (:5433, FRE-375). Apply the migration first::

    docker exec -i build-postgres-test-1 psql -U agent -d personal_agent \
        < docker/postgres/migrations/0038_dev_broker.sql
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import pytest_asyncio
from sqlalchemy.engine import make_url

from personal_agent.config import settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn

BROKER_ROLE = "seshat_dev_broker"
BROKER_DEV_PASSWORD = "seshat_dev_broker_dev_password"


def broker_dsn() -> str:
    """Return the test-stack DSN for the broker's role."""
    url = make_url(_normalize_asyncpg_dsn(settings.database_url))
    return url.set(username=BROKER_ROLE, password=BROKER_DEV_PASSWORD).render_as_string(
        hide_password=False
    )


@pytest_asyncio.fixture
async def admin_pool() -> AsyncIterator[asyncpg.Pool]:
    """A pool as ``agent``, for what the gateway writes and for cleanup."""
    pool = await asyncpg.create_pool(_normalize_asyncpg_dsn(settings.database_url), min_size=1, max_size=4)
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def broker_pool() -> AsyncIterator[asyncpg.Pool]:
    """A pool as ``seshat_dev_broker``."""
    pool = await asyncpg.create_pool(broker_dsn(), min_size=1, max_size=12)
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


async def delete_job_rows(pool: asyncpg.Pool, job_id: UUID) -> None:
    """Remove one job's token, counter and call rows."""
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM dev_job_calls WHERE job_id = $1", job_id)
        await conn.execute("DELETE FROM dev_job_spend WHERE job_id = $1", job_id)
        await conn.execute("DELETE FROM dev_job_tokens WHERE job_id = $1", job_id)
