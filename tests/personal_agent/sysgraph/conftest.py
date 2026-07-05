"""Test fixtures for the sysgraph repository and isolation proofs (ADR-0105 / FRE-714)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest_asyncio

from personal_agent.config import settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn
from personal_agent.sysgraph import SysgraphRepository


def _role_dsn(role: str, password: str) -> str:
    """Swap credentials on the test Postgres URL, keeping host/port/db.

    Ties every role-specific test DSN to whatever host/port
    ``settings.database_url`` already resolves to (the test stack, :5433),
    instead of hardcoding a port that would drift from ``tests/conftest.py``.
    """
    base = urlsplit(_normalize_asyncpg_dsn(settings.database_url))
    netloc = f"{role}:{password}@{base.hostname}:{base.port}"
    return urlunsplit((base.scheme, netloc, base.path, base.query, base.fragment))


@pytest_asyncio.fixture
async def sysgraph_repo() -> AsyncIterator[SysgraphRepository]:
    """A connected ``SysgraphRepository`` against the running test Postgres."""
    repo = SysgraphRepository(dsn=settings.sysgraph_database_url)
    await repo.connect()
    try:
        yield repo
    finally:
        await repo.disconnect()


@pytest_asyncio.fixture
async def sysgraph_pool() -> AsyncIterator[asyncpg.Pool]:
    """Direct asyncpg pool as ``sysgraph_role``, for seeding/cleaning test rows."""
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.sysgraph_database_url),
        min_size=1,
        max_size=2,
        command_timeout=10,
    )
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def agent_pool() -> AsyncIterator[asyncpg.Pool]:
    """Direct asyncpg pool as ``agent`` (the migration-running superuser)."""
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.database_url),
        min_size=1,
        max_size=1,
        command_timeout=10,
    )
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def recall_role_pool() -> AsyncIterator[asyncpg.Pool]:
    """Direct asyncpg pool as ``recall_role`` — stands in for the recall/user-facing connection."""
    dsn = _role_dsn("recall_role", "recall_dev_password")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1, command_timeout=10)
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()
