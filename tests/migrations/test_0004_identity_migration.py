"""ADR-0074 / FRE-376 — migration 0004 (traceability identity) integration test.

The migration is exercised inside an ephemeral schema in the test-stack
Postgres so the assertions are real (not text-pattern grep). This avoids
contaminating the default ``public`` schema and skips cleanly if the test
stack isn't running (``make test-infra-up`` provides it).
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
    / "0004_traceability_identity.sql"
)


def _strip_transaction_wrapper(sql: str) -> str:
    """Drop ``BEGIN;`` / ``COMMIT;`` so we can run the body inside our own tx.

    The migration file ships its own ``BEGIN; … COMMIT;`` so a one-shot
    ``psql -f`` works against prod. When we execute it inside the test
    schema we're already inside our own ``set_session_schema`` transaction,
    and asyncpg refuses nested transaction statements.
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
    except Exception as exc:
        pytest.skip(f"test-stack Postgres unavailable ({exc}); run `make test-infra-up`")
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}")
        yield conn, schema
    finally:
        await conn.execute("SET search_path TO public")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@pytest.mark.asyncio
async def test_migration_drops_null_trace_rows_and_adds_columns(
    ephemeral_schema,
) -> None:
    """Migration purges NULL-trace rows, flips trace_id NOT NULL, adds session_id."""
    conn, schema = ephemeral_schema

    # Seed a minimal pre-0004 schema mirroring what prod looked like before the
    # migration: api_costs with NULL-able trace_id and no session_id; sessions
    # without primary_model_at_creation / model_config_path.
    await conn.execute(
        """
        CREATE TABLE api_costs (
            id BIGSERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            provider VARCHAR(50) NOT NULL,
            model VARCHAR(100) NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd DECIMAL(10, 6) NOT NULL DEFAULT 0,
            trace_id UUID,
            purpose VARCHAR(50),
            latency_ms INTEGER
        );
        CREATE TABLE sessions (
            session_id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            mode VARCHAR(20) NOT NULL DEFAULT 'NORMAL',
            channel VARCHAR(50),
            metadata JSONB DEFAULT '{}',
            messages JSONB DEFAULT '[]'
        );
        """
    )
    # Two legacy rows: one NULL trace, one with a trace id (kept).
    await conn.execute(
        "INSERT INTO api_costs (provider, model, trace_id) "
        "VALUES ('anthropic', 'claude', NULL), ('openai', 'gpt', $1)",
        uuid4(),
    )
    pre_count = await conn.fetchval("SELECT count(*) FROM api_costs")
    assert pre_count == 2

    migration_sql = _strip_transaction_wrapper(MIGRATION_PATH.read_text())
    await conn.execute(migration_sql)

    # NULL row purged, attributable row kept.
    rows = await conn.fetch("SELECT trace_id FROM api_costs ORDER BY id")
    assert len(rows) == 1
    assert rows[0]["trace_id"] is not None

    # trace_id is now NOT NULL — direct insert should fail.
    with pytest.raises(asyncpg.NotNullViolationError):
        await conn.execute(
            "INSERT INTO api_costs (provider, model, trace_id) VALUES ('x', 'y', NULL)"
        )

    # session_id column present and NULL-able.
    api_cols = {
        r["column_name"]: r["is_nullable"]
        for r in await conn.fetch(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = 'api_costs'",
            schema,
        )
    }
    assert api_cols.get("session_id") == "YES"
    assert api_cols.get("trace_id") == "NO"

    # Sessions attribution columns added.
    session_cols = {
        r["column_name"]
        for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = 'sessions'",
            schema,
        )
    }
    assert "primary_model_at_creation" in session_cols
    assert "model_config_path" in session_cols
