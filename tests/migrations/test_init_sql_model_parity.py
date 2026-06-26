"""FRE-606 — schema-parity guard: SQLAlchemy models ↔ init.sql.

Generalizes the point-in-time FRE-591 fix (``sessions.user_id`` divergence) into
a standing guard. ``Base.metadata.create_all`` only creates *missing tables*,
never a column on an existing table, so any ``Column(...)`` added to a model
without a matching ``docker/postgres/init.sql`` edit silently reintroduces the
FRE-591 class of bug — invisible until a fresh ``make up`` / ``make test-infra-up``
/ DR rebuild breaks on the first INSERT.

This test builds a Postgres schema **only** from ``init.sql`` (mirrors the
``make test-infra-up`` entrypoint path — no ``create_all``) inside an ephemeral
schema in the test-stack DB, then asserts every column declared by every
SQLAlchemy model has a counterpart in that schema. The assertions are real SQL
introspection, not a text grep over init.sql. Skips cleanly if the test stack
isn't running (``make test-infra-up``).

Run it::

    make test-infra-up                 # start the isolated test Postgres (:5433)
    make test-file FILE=tests/migrations/test_init_sql_model_parity.py
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.config import settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn
from personal_agent.service.models import Base

INIT_SQL_PATH = Path(__file__).resolve().parents[2] / "docker" / "postgres" / "init.sql"


def _model_columns() -> dict[str, set[str]]:
    """Map each SQLAlchemy model table to the column names it declares.

    Returns:
        ``{table_name: {column_name, ...}}`` for every table registered on
        ``Base.metadata`` (the durable-storage ORM models). Column names are the
        DB-level names (e.g. ``SessionModel.metadata_`` maps to ``metadata``).
    """
    return {
        table.name: {column.name for column in table.columns}
        for table in Base.metadata.tables.values()
    }


async def _db_columns(conn: asyncpg.Connection, schema: str, table: str) -> set[str]:
    """Return the column names of ``schema.table`` from ``information_schema``."""
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = $1 AND table_name = $2",
        schema,
        table,
    )
    return {row["column_name"] for row in rows}


async def _missing_model_columns(conn: asyncpg.Connection, schema: str) -> list[str]:
    """Find model columns with no counterpart in the init.sql-built schema.

    Args:
        conn: Connection whose ``search_path`` includes ``schema``.
        schema: Ephemeral schema already populated from ``init.sql``.

    Returns:
        Sorted ``"table.column"`` identifiers for every model-declared column
        absent from the live schema. Empty when models and init.sql agree. The
        reverse direction (init.sql columns/tables the ORM does not model, e.g.
        ``api_costs``, ``route_traces``) is intentional and not reported.
    """
    missing: list[str] = []
    for table, columns in _model_columns().items():
        db_columns = await _db_columns(conn, schema, table)
        missing.extend(f"{table}.{column}" for column in columns - db_columns)
    return sorted(missing)


async def _apply_init_sql(conn: asyncpg.Connection) -> None:
    """Run the full ``init.sql`` against the connection's current schema."""
    await conn.execute(INIT_SQL_PATH.read_text())


@pytest_asyncio.fixture
async def init_sql_schema():
    """Build a one-shot schema from ``init.sql``; drop it at teardown.

    ``search_path`` is set to ``<schema>, public`` so unqualified ``CREATE TABLE``
    lands in the ephemeral schema (its name shadows any public table) while the
    ``vector`` type and ``CREATE EXTENSION IF NOT EXISTS vector`` resolve from the
    extension's ``public`` install.
    """
    dsn = _normalize_asyncpg_dsn(settings.database_url)
    schema = f"parity_test_{uuid4().hex[:8]}"
    try:
        conn = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"test-stack Postgres unavailable ({exc}); run `make test-infra-up`")
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}, public")
        await _apply_init_sql(conn)
        yield conn, schema
    finally:
        await conn.execute("SET search_path TO public")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@pytest.mark.asyncio
async def test_every_model_column_exists_in_init_sql(init_sql_schema) -> None:
    """No SQLAlchemy model column may be missing from the init.sql-built schema.

    This is the standing guard: it passes on current ``main`` (FRE-591 landed)
    and fails the moment a future ``Column(...)`` is added to a model without the
    matching ``init.sql`` edit.
    """
    conn, schema = init_sql_schema

    missing = await _missing_model_columns(conn, schema)

    assert missing == [], (
        "SQLAlchemy model columns with no init.sql counterpart "
        f"(add them to docker/postgres/init.sql + a migration): {missing}"
    )


@pytest.mark.asyncio
async def test_guard_detects_dropped_column(init_sql_schema) -> None:
    """Synthetic drift reproduces the FRE-591 gap: dropping ``sessions.user_id``.

    Removing the column from the live schema (while the model still declares it)
    is exactly the divergence FRE-591 fixed — the guard must flag it.
    """
    conn, schema = init_sql_schema

    await conn.execute("ALTER TABLE sessions DROP COLUMN user_id")

    missing = await _missing_model_columns(conn, schema)

    assert "sessions.user_id" in missing
