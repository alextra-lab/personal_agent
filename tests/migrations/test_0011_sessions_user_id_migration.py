"""FRE-591 — migration 0011 (sessions.user_id schema divergence) integration test.

``SessionModel.user_id`` is declared NOT NULL + FK in the SQLAlchemy model and
inserted by ``SessionRepository.create``, but no ``user_id`` column was ever
created in ``docker/postgres/init.sql`` or any migration. ``create_all`` only
creates missing tables, so a fresh volume yielded a ``sessions`` table without
``user_id`` and the first session INSERT failed. Migration 0011 + the init.sql
mirror fix the divergence; this test exercises the migration inside an ephemeral
schema in the test-stack Postgres so the assertions are real (not text grep).
Skips cleanly if the test stack isn't running (``make test-infra-up``).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.config import settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "docker"
    / "postgres"
    / "migrations"
    / "0011_sessions_user_id.sql"
)

# Pre-0011 schema: ``users`` plus a ``sessions`` table WITHOUT ``user_id`` —
# exactly what a fresh init.sql volume produced before this fix.
_SEED_USERS = """
    CREATE TABLE users (
        user_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        email        TEXT NOT NULL UNIQUE,
        display_name TEXT NULL,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
"""
_SEED_SESSIONS = """
    CREATE TABLE sessions (
        session_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        mode           VARCHAR(20) NOT NULL DEFAULT 'NORMAL',
        channel        VARCHAR(50),
        metadata       JSONB DEFAULT '{}',
        messages       JSONB DEFAULT '[]',
        primary_model_at_creation VARCHAR(120),
        model_config_path         VARCHAR(255),
        execution_profile         VARCHAR(50) NOT NULL DEFAULT 'local'
    );
"""


def _strip_transaction_wrapper(sql: str) -> str:
    """Drop ``BEGIN;`` / ``COMMIT;`` so the body runs inside our own tx.

    The migration ships its own ``BEGIN; … COMMIT;`` so a one-shot ``psql -f``
    works against prod. Executed inside the ephemeral-schema transaction we're
    already in one, and asyncpg refuses nested transaction statements.
    """
    return "\n".join(
        line for line in sql.splitlines() if line.strip().upper() not in {"BEGIN;", "COMMIT;"}
    )


@pytest_asyncio.fixture
async def ephemeral_schema():
    """Create a one-shot schema in the test-stack DB, drop it at teardown."""
    dsn = _normalize_asyncpg_dsn(settings.database_url)
    schema = f"migration_test_{uuid4().hex[:8]}"
    try:
        conn = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"test-stack Postgres unavailable ({exc}); run `make test-infra-up`")
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}")
        yield conn, schema
    finally:
        await conn.execute("SET search_path TO public")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


async def _columns(conn: asyncpg.Connection, schema: str, table: str) -> dict[str, str]:
    return {
        r["column_name"]: r["is_nullable"]
        for r in await conn.fetch(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = $2",
            schema,
            table,
        )
    }


@pytest.mark.asyncio
async def test_migration_adds_user_id_notnull_fk_on_empty_sessions(
    ephemeral_schema,
) -> None:
    """Migration adds NOT NULL user_id + ix_sessions_user_id + FK on empty sessions."""
    conn, schema = ephemeral_schema
    await conn.execute(_SEED_USERS)
    await conn.execute(_SEED_SESSIONS)

    migration_sql = _strip_transaction_wrapper(MIGRATION_PATH.read_text())
    await conn.execute(migration_sql)

    # Column present and NOT NULL (no orphan rows → SET NOT NULL applied).
    session_cols = await _columns(conn, schema, "sessions")
    assert session_cols.get("user_id") == "NO"

    # Index created with the prod/SQLAlchemy name.
    idx = await conn.fetchval(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = $1 AND tablename = 'sessions' AND indexname = 'ix_sessions_user_id'",
        schema,
    )
    assert idx == "ix_sessions_user_id"

    # FK constraint present and pointing at users(user_id).
    fk = await conn.fetchval(
        "SELECT conname FROM pg_constraint "
        "WHERE conname = 'sessions_user_id_fkey' "
        "AND connamespace = $1::regnamespace AND contype = 'f'",
        schema,
    )
    assert fk == "sessions_user_id_fkey"

    # NOT NULL enforced — insert without user_id fails.
    with pytest.raises(asyncpg.NotNullViolationError):
        await conn.execute("INSERT INTO sessions (created_at) VALUES (NOW())")

    # FK enforced — insert with an unknown user_id fails.
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute("INSERT INTO sessions (user_id) VALUES ($1)", uuid4())

    # Happy path — a real user lets a session insert succeed.
    user_id = await conn.fetchval(
        "INSERT INTO users (email) VALUES ('fre591@test.local') RETURNING user_id"
    )
    await conn.execute("INSERT INTO sessions (user_id) VALUES ($1)", user_id)
    assert await conn.fetchval("SELECT count(*) FROM sessions") == 1


@pytest.mark.asyncio
async def test_migration_is_idempotent(ephemeral_schema) -> None:
    """Applying 0011 twice is a no-op the second time (mirrors prod)."""
    conn, schema = ephemeral_schema
    await conn.execute(_SEED_USERS)
    await conn.execute(_SEED_SESSIONS)

    migration_sql = _strip_transaction_wrapper(MIGRATION_PATH.read_text())
    await conn.execute(migration_sql)
    await conn.execute(migration_sql)  # second apply must not raise

    session_cols = await _columns(conn, schema, "sessions")
    assert session_cols.get("user_id") == "NO"
    # No duplicate FK or index created on re-apply.
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conname = 'sessions_user_id_fkey' AND connamespace = $1::regnamespace",
            schema,
        )
        == 1
    )
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM pg_indexes "
            "WHERE schemaname = $1 AND indexname = 'ix_sessions_user_id'",
            schema,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_migration_leaves_nullable_when_orphan_rows(ephemeral_schema) -> None:
    """Orphan (column-less) rows block SET NOT NULL — column stays nullable, no crash."""
    conn, schema = ephemeral_schema
    await conn.execute(_SEED_USERS)
    await conn.execute(_SEED_SESSIONS)
    # A pre-0011 session row that cannot carry a user_id yet.
    await conn.execute("INSERT INTO sessions (created_at) VALUES (NOW())")

    migration_sql = _strip_transaction_wrapper(MIGRATION_PATH.read_text())
    await conn.execute(migration_sql)  # must not raise despite the orphan row

    session_cols = await _columns(conn, schema, "sessions")
    # Column added but left NULLABLE (refuses to auto-attribute orphans).
    assert session_cols.get("user_id") == "YES"
