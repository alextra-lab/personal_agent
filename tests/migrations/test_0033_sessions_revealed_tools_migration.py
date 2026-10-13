"""FRE-1595 (ADR-0156 A5.4) — migration 0033 idempotency and init.sql parity.

Two ephemeral schemas in the test-stack Postgres:

* schema A holds ``sessions`` built from the ``CREATE TABLE`` statement in ``init.sql``;
* schema B holds the same statement without the ``revealed_tools`` column (the shape before
  0033), then migration 0033.

The two schemas must have the same columns, types, nullability and defaults, and applying 0033
a second time must change nothing. Skips cleanly if the test stack is not running
(``make test-infra-up``).
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
_MIGRATION = _DOCKER_DIR / "migrations" / "0033_sessions_revealed_tools.sql"

_COLUMNS_SQL = """
    SELECT column_name, data_type, udt_name, character_maximum_length,
           is_nullable, column_default
    FROM information_schema.columns
    WHERE table_schema = $1 AND table_name = 'sessions'
    ORDER BY column_name
"""


def _init_sql_sessions_statement() -> str:
    """Return the ``CREATE TABLE ... sessions`` statement from ``init.sql``."""
    text = (_DOCKER_DIR / "init.sql").read_text()
    match = re.search(r"CREATE TABLE IF NOT EXISTS sessions \(.*?\n\);\n", text, flags=re.DOTALL)
    assert match is not None, "sessions CREATE TABLE not found in init.sql"
    return match.group(0)


def _pre_0033_statement() -> str:
    """The init.sql statement without the revealed_tools column and its comment lines."""
    statement = _init_sql_sessions_statement()
    # The column is the last one: drop its comment block and line, and the comma before it.
    stripped, count = re.subn(
        r",\n(?:\s*--[^\n]*\n)*\s*revealed_tools [^\n]*\n", "\n", statement, flags=re.DOTALL
    )
    assert count == 1, "revealed_tools column not found in the init.sql sessions statement"
    return stripped


@pytest_asyncio.fixture
async def two_schemas():
    """Yield ``(conn, schema_a, schema_b)``; drop both at teardown."""
    dsn = _normalize_asyncpg_dsn(settings.database_admin_url)
    suffix = uuid4().hex[:8]
    schema_a, schema_b = f"mig1595_init_{suffix}", f"mig1595_chain_{suffix}"
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


async def _columns(conn: asyncpg.Connection, schema: str) -> list[tuple[object, ...]]:
    return [tuple(r.values()) for r in await conn.fetch(_COLUMNS_SQL, schema)]


@pytest.mark.asyncio
async def test_migration_matches_init_sql(two_schemas) -> None:
    """0033 on the pre-0033 table builds the same ``sessions`` columns as ``init.sql``."""
    conn, schema_a, schema_b = two_schemas
    await conn.execute(f"SET search_path TO {schema_a}")
    await conn.execute(_init_sql_sessions_statement())
    await conn.execute(f"SET search_path TO {schema_b}")
    await conn.execute(_pre_0033_statement())
    assert "revealed_tools" not in {row[0] for row in await _columns(conn, schema_b)}
    await conn.execute(_MIGRATION.read_text())

    from_init = await _columns(conn, schema_a)
    assert await _columns(conn, schema_b) == from_init
    column = next(row for row in from_init if row[0] == "revealed_tools")
    assert column[1:5] == ("ARRAY", "_text", None, "NO")
    assert column[5] == "'{}'::text[]"


@pytest.mark.asyncio
async def test_migration_0033_is_idempotent(two_schemas) -> None:
    """Applying 0033 a second time raises no error and changes nothing."""
    conn, _schema_a, schema_b = two_schemas
    await conn.execute(f"SET search_path TO {schema_b}")
    await conn.execute(_pre_0033_statement())
    await conn.execute(_MIGRATION.read_text())
    before = await _columns(conn, schema_b)

    await conn.execute(f"SET search_path TO {schema_b}")
    await conn.execute(_MIGRATION.read_text())
    assert await _columns(conn, schema_b) == before


@pytest.mark.asyncio
async def test_existing_rows_get_an_empty_set(two_schemas) -> None:
    """A session that exists before 0033 reads an empty set, not NULL."""
    conn, _schema_a, schema_b = two_schemas
    await conn.execute(f"SET search_path TO {schema_b}")
    await conn.execute(_pre_0033_statement())
    await conn.execute("INSERT INTO sessions (user_id) VALUES (gen_random_uuid())")
    await conn.execute(_MIGRATION.read_text())
    assert await conn.fetchval("SELECT revealed_tools FROM sessions") == []
