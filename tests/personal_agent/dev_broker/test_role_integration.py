# ruff: noqa: D102
"""The broker's database role reaches only its own tables (ADR-0159 D4.4; FRE-1683 AC-5).

Connects to the test stack as ``seshat_dev_broker``. ``dev_jobs`` arrives with FRE-1684. Until
then, its refusal rests on the role having no ``ALTER DEFAULT PRIVILEGES`` (checked in
``test_schema_text.py``); this test checks it too as soon as the table exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from .conftest import broker_dsn

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    """One connection as the broker's role."""
    connection = await asyncpg.connect(broker_dsn())
    try:
        yield connection
    finally:
        await connection.close()


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1 FROM artifacts LIMIT 1",
        "SELECT 1 FROM users LIMIT 1",
        "SELECT 1 FROM sessions LIMIT 1",
        "SELECT 1 FROM session_events LIMIT 1",
        "SELECT 1 FROM settings_overrides LIMIT 1",
        # api_costs: insert only. Reading other rows' costs is refused.
        "SELECT cost_usd FROM api_costs LIMIT 1",
        # Only the gateway issues a token, and a token's job, model and expiry are fixed.
        "INSERT INTO dev_job_tokens (token_hash, job_id, model_key, trace_id, session_id, status,"
        " expires_at) VALUES ('x', gen_random_uuid(), 'm', gen_random_uuid(), gen_random_uuid(),"
        " 'active', NOW())",
        "UPDATE dev_job_tokens SET model_key = 'glm-5.3-scaleway'",
        "UPDATE dev_job_tokens SET expires_at = NOW() + INTERVAL '1 year'",
        "DELETE FROM dev_job_calls",
        "DELETE FROM budget_reservations",
        "CREATE TABLE broker_owned (id INT)",
    ],
)
async def test_the_role_is_refused(conn: asyncpg.Connection, query: str) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.execute(query)


async def test_dev_jobs_is_refused_once_it_exists(conn: asyncpg.Connection) -> None:
    exists = await conn.fetchval("SELECT to_regclass('public.dev_jobs') IS NOT NULL")
    if not exists:
        pytest.skip("dev_jobs arrives with FRE-1684")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.execute("SELECT 1 FROM dev_jobs LIMIT 1")


async def test_the_role_is_not_a_superuser(conn: asyncpg.Connection) -> None:
    row = await conn.fetchrow(
        "SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls FROM pg_roles "
        "WHERE rolname = current_user"
    )
    assert dict(row) == {
        "rolsuper": False,
        "rolcreaterole": False,
        "rolcreatedb": False,
        "rolbypassrls": False,
    }
