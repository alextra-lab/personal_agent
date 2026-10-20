"""FRE-1622 (ADR-0157 D2) — migration 0034: sessions.seshat_effort.

Two ephemeral schemas in the test-stack Postgres:

* schema A holds ``sessions`` built from the ``CREATE TABLE`` statement in ``init.sql``;
* schema B holds the same statement without the ``seshat_effort`` column and its check (the
  shape before 0034), then migration 0034.

The two must have the same columns and the same check. 0034 is idempotent, backfills every
existing row with ``standard``, and the database refuses a null and any value outside the three
levels. Skips cleanly if the test stack is not running (``make test-infra-up``).
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.config import settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn

_DOCKER_DIR = Path(__file__).resolve().parents[2] / "docker" / "postgres"
_MIGRATION = _DOCKER_DIR / "migrations" / "0034_sessions_seshat_effort.sql"

_COLUMNS_SQL = """
    SELECT column_name, data_type, udt_name, character_maximum_length,
           is_nullable, column_default
    FROM information_schema.columns
    WHERE table_schema = $1 AND table_name = 'sessions'
    ORDER BY column_name
"""
_CHECKS_SQL = """
    SELECT con.conname, pg_get_constraintdef(con.oid)
    FROM pg_constraint con
    JOIN pg_namespace ns ON ns.oid = con.connamespace
    WHERE ns.nspname = $1 AND con.contype = 'c'
    ORDER BY con.conname
"""


def init_sql_sessions_statement() -> str:
    """Return the ``CREATE TABLE ... sessions`` statement from ``init.sql``."""
    text = (_DOCKER_DIR / "init.sql").read_text()
    match = re.search(r"CREATE TABLE IF NOT EXISTS sessions \(.*?\n\);\n", text, flags=re.DOTALL)
    assert match is not None, "sessions CREATE TABLE not found in init.sql"
    return match.group(0)


def pre_0034_statement() -> str:
    """The init.sql statement without the seshat_effort column, its comment and its check."""
    stripped, count = re.subn(
        r",\n(?:\s*--[^\n]*\n)*\s*seshat_effort [^\n]*\n\s*CONSTRAINT ck_sessions_seshat_effort"
        r" [^\n]*\n",
        "\n",
        init_sql_sessions_statement(),
        flags=re.DOTALL,
    )
    assert count == 1, "seshat_effort column not found in the init.sql sessions statement"
    return stripped


@pytest_asyncio.fixture
async def two_schemas():
    """Yield ``(conn, schema_a, schema_b)``; drop both at teardown."""
    dsn = _normalize_asyncpg_dsn(settings.database_admin_url)
    suffix = uuid4().hex[:8]
    schema_a, schema_b = f"mig1622_init_{suffix}", f"mig1622_chain_{suffix}"
    try:
        conn = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"test-stack Postgres unavailable ({exc}); run `make test-infra-up`")
    try:
        await conn.execute(f"CREATE SCHEMA {schema_a}")
        await conn.execute(f"CREATE SCHEMA {schema_b}")
        yield conn, schema_a, schema_b
    finally:
        await conn.execute("SET search_path TO public")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema_a} CASCADE")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema_b} CASCADE")
        await conn.close()


async def _shape(conn: asyncpg.Connection, schema: str) -> tuple[list[object], list[object]]:
    columns = [tuple(r.values()) for r in await conn.fetch(_COLUMNS_SQL, schema)]
    checks = [tuple(r.values()) for r in await conn.fetch(_CHECKS_SQL, schema)]
    return columns, checks


async def _chain(conn: asyncpg.Connection, schema: str) -> None:
    await conn.execute(f"SET search_path TO {schema}")
    await conn.execute(pre_0034_statement())
    await conn.execute(_MIGRATION.read_text())


@pytest.mark.asyncio
async def test_migration_matches_init_sql(two_schemas) -> None:
    conn, schema_a, schema_b = two_schemas
    await conn.execute(f"SET search_path TO {schema_a}")
    await conn.execute(init_sql_sessions_statement())
    await _chain(conn, schema_b)

    from_init = await _shape(conn, schema_a)
    assert await _shape(conn, schema_b) == from_init
    column = next(row for row in from_init[0] if row[0] == "seshat_effort")
    assert column[1:5] == ("character varying", "varchar", 16, "NO")


@pytest.mark.asyncio
async def test_migration_0034_is_idempotent(two_schemas) -> None:
    conn, _schema_a, schema_b = two_schemas
    await _chain(conn, schema_b)
    before = await _shape(conn, schema_b)
    await conn.execute(_MIGRATION.read_text())
    assert await _shape(conn, schema_b) == before


@pytest.mark.asyncio
async def test_existing_rows_are_backfilled_with_standard(two_schemas) -> None:
    conn, _schema_a, schema_b = two_schemas
    await conn.execute(f"SET search_path TO {schema_b}")
    await conn.execute(pre_0034_statement())
    await conn.execute("INSERT INTO sessions (user_id) VALUES (gen_random_uuid())")
    await conn.execute(_MIGRATION.read_text())
    assert await conn.fetchval("SELECT seshat_effort FROM sessions") == "standard"
    assert await conn.fetchval("SELECT count(*) FROM sessions WHERE seshat_effort IS NULL") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "turbo"])
async def test_the_database_refuses_a_null_and_an_unknown_level(two_schemas, value) -> None:
    conn, _schema_a, schema_b = two_schemas
    await _chain(conn, schema_b)
    expected = asyncpg.NotNullViolationError if value is None else asyncpg.CheckViolationError
    with pytest.raises(expected):
        await conn.execute(
            "INSERT INTO sessions (user_id, seshat_effort) VALUES (gen_random_uuid(), $1)", value
        )
    await conn.execute(
        "INSERT INTO sessions (user_id, seshat_effort) VALUES (gen_random_uuid(), 'deep')"
    )
    with pytest.raises(expected):
        await conn.execute("UPDATE sessions SET seshat_effort = $1", value)
    assert await conn.fetchval("SELECT seshat_effort FROM sessions") == "deep"
