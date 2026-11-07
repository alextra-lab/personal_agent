"""Fixtures for the job record and supervisor tests (FRE-1684).

The database tests run on the test stack (:5433, FRE-375) as the gateway's role ``seshat_app``, so a
passing test also proves that role's grants. Apply the migrations first (as ``agent``)::

    docker exec -i build-postgres-test-1 psql -U agent -d personal_agent \
        < docker/postgres/migrations/0039_dev_jobs.sql

``dev_jobs_one_active`` allows one active job in the whole database, so every test job carries the
approval prefix below, and the fixture deletes those rows before and after each test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from personal_agent.config import settings
from personal_agent.dev_agent.jobs import DevJob, JobStore
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn

#: Every test job's approval id starts with this, so cleanup touches test rows only.
TEST_APPROVAL_PREFIX = "fre1684-test-"

MakeJob = Callable[..., Awaitable[DevJob]]


async def delete_test_jobs(pool: asyncpg.Pool) -> None:
    """Remove every test job and its token, counter, call and action rows."""
    async with pool.acquire() as conn:
        ids = [
            r["id"]
            for r in await conn.fetch(
                "SELECT id FROM dev_jobs WHERE approval_request_id LIKE $1",
                TEST_APPROVAL_PREFIX + "%",
            )
        ]
        if not ids:
            return
        await conn.execute("DELETE FROM dev_job_actions WHERE job_id = ANY($1::uuid[])", ids)
        await conn.execute("DELETE FROM dev_job_calls WHERE job_id = ANY($1::uuid[])", ids)
        await conn.execute("DELETE FROM dev_job_spend WHERE job_id = ANY($1::uuid[])", ids)
        await conn.execute("DELETE FROM dev_job_tokens WHERE job_id = ANY($1::uuid[])", ids)
        await conn.execute("DELETE FROM dev_jobs WHERE id = ANY($1::uuid[])", ids)


@pytest_asyncio.fixture
async def admin_pool() -> AsyncIterator[asyncpg.Pool]:
    """A pool as ``seshat_app`` on the test stack. Test jobs are removed before and after the test."""
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.database_url), min_size=1, max_size=8
    )
    assert pool is not None
    try:
        if await pool.fetchval("SELECT to_regclass('public.dev_jobs')") is None:
            pytest.skip("dev_jobs is missing: apply migration 0039 to the test stack")
        await delete_test_jobs(pool)
        other = await pool.fetchval(
            "SELECT count(*) FROM dev_jobs WHERE state IN "
            "('queued','running','checking','delivering')"
        )
        if other:
            pytest.skip("a non-test job is active on the test stack, so the one-job slot is taken")
        yield pool
        await delete_test_jobs(pool)
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def store(admin_pool: asyncpg.Pool) -> JobStore:
    """The job store on the test stack."""
    return JobStore(admin_pool)


@pytest_asyncio.fixture
async def make_job(store: JobStore) -> MakeJob:
    """Create a ``queued`` test job."""

    async def make(**overrides: object) -> DevJob:
        values: dict[str, object] = {
            "job_id": uuid4(),
            "user_id": uuid4(),
            "session_id": uuid4(),
            "trace_id": uuid4(),
            "approval_request_id": f"{TEST_APPROVAL_PREFIX}{uuid4()}",
            "mode": "build",
            "task_text": "Build a page with one working button.",
            "model_id": "deepseek-v4.1-flash-scaleway",
        }
        values.update(overrides)
        return await store.create_job(**values)  # type: ignore[arg-type]

    return make


async def expire_lease(pool: asyncpg.Pool, job_id: UUID) -> None:
    """Seed an expired lease: the expiry moves one second into the past."""
    await pool.execute(
        "UPDATE dev_jobs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE id = $1",
        job_id,
    )
