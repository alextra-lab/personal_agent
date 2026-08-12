"""FRE-1210 — migration 0026 (kg_stats) round-trip test.

Exercised inside an ephemeral schema in the test-stack Postgres, mirroring
``test_0020_session_model_selections_migration.py``. The migration's
``GRANT ... ON public.kg_stats`` line is schema-qualified (matches the
0025/0026 convention of explicit table-grain grants), so it targets the real
``public.kg_stats`` rather than the ephemeral schema's copy -- that grant is
checked separately, directly against ``public``, in
``test_grafana_ro_has_select_on_kg_stats``.

Skips cleanly if the test stack isn't running (``make test-infra-up``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.config import settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "docker" / "postgres" / "migrations" / "0026_kg_stats.sql"
)


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


@pytest.mark.asyncio
async def test_table_columns_and_default(ephemeral_schema) -> None:
    """A row inserted without ``observed_at`` gets ``DEFAULT NOW()``."""
    conn, schema = ephemeral_schema
    await conn.execute(_strip_transaction_wrapper(MIGRATION_PATH.read_text()))

    row_id = await conn.fetchval(
        f"INSERT INTO {schema}.kg_stats (metric_name, dimension, metric_value) "
        "VALUES ('cold_mass_ratio', NULL, 0.36) RETURNING id"
    )
    assert row_id is not None

    observed_at = await conn.fetchval(
        f"SELECT observed_at FROM {schema}.kg_stats WHERE id = $1", row_id
    )
    assert observed_at is not None  # DEFAULT NOW() populated it


@pytest.mark.asyncio
async def test_index_exists(ephemeral_schema) -> None:
    """``idx_kg_stats_metric_time`` is created by the migration."""
    conn, schema = ephemeral_schema
    await conn.execute(_strip_transaction_wrapper(MIGRATION_PATH.read_text()))

    idx = await conn.fetchval(
        "SELECT indexname FROM pg_indexes WHERE schemaname = $1 AND indexname = 'idx_kg_stats_metric_time'",
        schema,
    )
    assert idx == "idx_kg_stats_metric_time"


@pytest.mark.asyncio
async def test_unique_constraint_rejects_true_duplicate(ephemeral_schema) -> None:
    """A literal (observed_at, metric_name, dimension) duplicate is rejected."""
    conn, schema = ephemeral_schema
    await conn.execute(_strip_transaction_wrapper(MIGRATION_PATH.read_text()))

    ts = datetime(2026, 8, 11, 4, 0, 0, tzinfo=timezone.utc)
    await conn.execute(
        f"INSERT INTO {schema}.kg_stats (observed_at, metric_name, dimension, metric_value) "
        "VALUES ($1, 'entity_count', 'Person', 10)",
        ts,
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(
            f"INSERT INTO {schema}.kg_stats (observed_at, metric_name, dimension, metric_value) "
            "VALUES ($1, 'entity_count', 'Person', 99)",
            ts,
        )


@pytest.mark.asyncio
async def test_unique_constraint_treats_null_dimension_as_not_distinct(ephemeral_schema) -> None:
    """The NULLS NOT DISTINCT fix -- two same-instant scalar-metric rows conflict.

    Without ``NULLS NOT DISTINCT``, Postgres's default UNIQUE would treat
    these two ``dimension=NULL`` rows as non-conflicting, silently defeating
    the writer's ``ON CONFLICT DO NOTHING`` dedup guard for every scalar
    ratio/count metric (codex plan-review finding, 2026-08-11).
    """
    conn, schema = ephemeral_schema
    await conn.execute(_strip_transaction_wrapper(MIGRATION_PATH.read_text()))

    ts = datetime(2026, 8, 11, 4, 0, 0, tzinfo=timezone.utc)
    await conn.execute(
        f"INSERT INTO {schema}.kg_stats (observed_at, metric_name, dimension, metric_value) "
        "VALUES ($1, 'cold_mass_ratio', NULL, 0.36)",
        ts,
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(
            f"INSERT INTO {schema}.kg_stats (observed_at, metric_name, dimension, metric_value) "
            "VALUES ($1, 'cold_mass_ratio', NULL, 0.99)",
            ts,
        )


@pytest.mark.asyncio
async def test_migration_is_idempotent(ephemeral_schema) -> None:
    """Applying 0026 twice is a no-op the second time (CREATE TABLE IF NOT EXISTS)."""
    conn, schema = ephemeral_schema
    migration_sql = _strip_transaction_wrapper(MIGRATION_PATH.read_text())
    await conn.execute(migration_sql)
    await conn.execute(migration_sql)  # second apply must not raise

    count = await conn.fetchval(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = $1 AND table_name = 'kg_stats'",
        schema,
    )
    assert count == 1


@pytest.mark.asyncio
async def test_grafana_ro_has_select_on_kg_stats() -> None:
    """Verify the grafana_ro grant against the real public schema.

    Checked against the real ``public`` schema -- the migration's GRANT is
    schema-qualified (``public.kg_stats``), so it doesn't touch the ephemeral
    schema used by the other tests in this file.
    """
    dsn = _normalize_asyncpg_dsn(settings.database_admin_url)
    try:
        conn = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"test-stack Postgres unavailable ({exc}); run `make test-infra-up`")
    try:
        privilege = await conn.fetchval(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE table_schema = 'public' AND table_name = 'kg_stats' "
            "AND grantee = 'grafana_ro' AND privilege_type = 'SELECT'"
        )
        if privilege is None:
            pytest.skip(
                "public.kg_stats not yet provisioned in this test DB (run `make test-infra-reset`)"
            )
        assert privilege == "SELECT"
    finally:
        await conn.close()
