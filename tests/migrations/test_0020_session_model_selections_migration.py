"""FRE-917 — migration 0020 (session_model_selections + backfill) integration test.

ADR-0121 §4 / AC-7: the selection store replaces execution-profile "Path" as the
source of truth for a session's primary model. The migration creates the table
AND backfills an EXPLICIT ``primary`` selection row per existing session, mapping
its stored ``execution_profile`` to the model that profile resolved to — so a
pre-existing session does not silently move when a default later changes.

Exercised inside an ephemeral schema in the test-stack Postgres, mirroring
``test_0019_sessions_purged_at_migration.py``. Skips cleanly if the test stack
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
    / "0020_session_model_selections.sql"
)

# Pre-0020 schema: users + sessions (with execution_profile), the current shape.
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
        user_id        UUID NOT NULL REFERENCES users(user_id),
        execution_profile VARCHAR(50) NOT NULL DEFAULT 'local'
    );
"""


def _strip_transaction_wrapper(sql: str) -> str:
    """Drop ``BEGIN;`` / ``COMMIT;`` so the body runs inside our own tx."""
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


async def _seed_session(conn: asyncpg.Connection, profile: str) -> object:
    user_id = await conn.fetchval(
        "INSERT INTO users (email) VALUES ($1) RETURNING user_id",
        f"fre917-{uuid4().hex[:8]}@test.local",
    )
    return await conn.fetchval(
        "INSERT INTO sessions (user_id, execution_profile) VALUES ($1, $2) RETURNING session_id",
        user_id,
        profile,
    )


@pytest.mark.asyncio
async def test_backfill_maps_each_profile_to_explicit_primary_row(ephemeral_schema) -> None:
    """AC-7a — local→qwen3.6-35b-thinking, cloud→claude_sonnet, one explicit row each."""
    conn, _schema = ephemeral_schema
    await conn.execute(_SEED_USERS)
    await conn.execute(_SEED_SESSIONS)

    local_sid = await _seed_session(conn, "local")
    cloud_sid = await _seed_session(conn, "cloud")

    await conn.execute(_strip_transaction_wrapper(MIGRATION_PATH.read_text()))

    local_key = await conn.fetchval(
        "SELECT deployment_key FROM session_model_selections "
        "WHERE session_id = $1 AND role = 'primary'",
        local_sid,
    )
    cloud_key = await conn.fetchval(
        "SELECT deployment_key FROM session_model_selections "
        "WHERE session_id = $1 AND role = 'primary'",
        cloud_sid,
    )
    assert local_key == "qwen3.6-35b-thinking"
    assert cloud_key == "claude_sonnet"

    # Exactly one primary row per session (explicit, not implicit).
    total = await conn.fetchval(
        "SELECT count(*) FROM session_model_selections WHERE role = 'primary'"
    )
    assert total == 2


@pytest.mark.asyncio
async def test_backfill_is_idempotent(ephemeral_schema) -> None:
    """Applying 0020 twice is a no-op the second time (ON CONFLICT DO NOTHING)."""
    conn, _schema = ephemeral_schema
    await conn.execute(_SEED_USERS)
    await conn.execute(_SEED_SESSIONS)
    sid = await _seed_session(conn, "cloud")

    migration_sql = _strip_transaction_wrapper(MIGRATION_PATH.read_text())
    await conn.execute(migration_sql)
    await conn.execute(migration_sql)  # second apply must not raise or duplicate

    count = await conn.fetchval(
        "SELECT count(*) FROM session_model_selections WHERE session_id = $1", sid
    )
    assert count == 1
