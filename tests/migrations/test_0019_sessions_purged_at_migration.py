"""FRE-860 — migration 0019 (sessions.purged_at retention column) integration test.

ADR-0098 D4/D6 (session-store retention): the ``sessions`` table has no retention/TTL/expiry
column, so nothing ages out session history. Migration 0019 adds a nullable ``purged_at``
column (soft-prune tombstone — see ``session_repository.prune_expired``) plus a partial index
scoping the retention sweep to not-yet-purged rows. This test exercises the migration inside
an ephemeral schema in the test-stack Postgres, mirroring
``tests/migrations/test_0011_sessions_user_id_migration.py``. Skips cleanly if the test stack
isn't running (``make test-infra-up``).
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
    / "0019_sessions_purged_at.sql"
)

# Pre-0019 schema: sessions table without purged_at — the current shipped shape.
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
        user_id        UUID NOT NULL REFERENCES users(user_id),
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
    dsn = _normalize_asyncpg_dsn(settings.database_admin_url)
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
async def test_migration_adds_nullable_purged_at_and_index(ephemeral_schema) -> None:
    """Migration adds a nullable purged_at column plus the retention-scan index."""
    conn, schema = ephemeral_schema
    await conn.execute(_SEED_USERS)
    await conn.execute(_SEED_SESSIONS)

    migration_sql = _strip_transaction_wrapper(MIGRATION_PATH.read_text())
    await conn.execute(migration_sql)

    session_cols = await _columns(conn, schema, "sessions")
    assert session_cols.get("purged_at") == "YES"

    idx = await conn.fetchval(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = $1 AND tablename = 'sessions' AND indexname = 'idx_sessions_retention_scan'",
        schema,
    )
    assert idx == "idx_sessions_retention_scan"

    # Existing rows default to NULL (not yet purged).
    user_id = await conn.fetchval(
        "INSERT INTO users (email) VALUES ('fre860@test.local') RETURNING user_id"
    )
    session_id = await conn.fetchval(
        "INSERT INTO sessions (user_id) VALUES ($1) RETURNING session_id", user_id
    )
    purged_at = await conn.fetchval(
        "SELECT purged_at FROM sessions WHERE session_id = $1", session_id
    )
    assert purged_at is None


@pytest.mark.asyncio
async def test_migration_is_idempotent(ephemeral_schema) -> None:
    """Applying 0019 twice is a no-op the second time (mirrors prod)."""
    conn, schema = ephemeral_schema
    await conn.execute(_SEED_USERS)
    await conn.execute(_SEED_SESSIONS)

    migration_sql = _strip_transaction_wrapper(MIGRATION_PATH.read_text())
    await conn.execute(migration_sql)
    await conn.execute(migration_sql)  # second apply must not raise

    session_cols = await _columns(conn, schema, "sessions")
    assert session_cols.get("purged_at") == "YES"
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM pg_indexes "
            "WHERE schemaname = $1 AND indexname = 'idx_sessions_retention_scan'",
            schema,
        )
        == 1
    )
