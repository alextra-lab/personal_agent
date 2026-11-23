"""FRE-1782 (ADR-0153 Amendment 1 L4) — migration 0042: artifact_share_links.

The upgrade path runs in a scratch database of the test-stack Postgres: the database is owned by
``agent``, the 0015 default privileges are in place, a minimal ``users`` and ``artifacts`` exist,
and 0042 then runs as ``agent``, as it does at deploy (FRE-808). The checks:

* the table built by 0042 has the same columns and checks as the ``init.sql`` statement;
* 0042 is idempotent;
* ``seshat_app`` can SELECT, INSERT and UPDATE the table and cannot DELETE it; ``grafana_ro`` and
  ``seshat_dev_broker`` get nothing (ticket AC-10);
* the table has no address or client column (V-L7 part 1, ticket AC-6).

Marker: ``integration``. Skips cleanly if the test stack is not running (``make test-infra-up``).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.config import settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_DOCKER_DIR = Path(__file__).resolve().parents[2] / "docker" / "postgres"
_MIGRATION = _DOCKER_DIR / "migrations" / "0042_artifact_share_links.sql"

_PREREQUISITES = """
    CREATE TABLE users (user_id UUID PRIMARY KEY, email TEXT NOT NULL UNIQUE);
    CREATE TABLE artifacts (id UUID PRIMARY KEY, user_id UUID NOT NULL REFERENCES users(user_id));
"""
_DEFAULT_PRIVILEGES = """
    GRANT USAGE ON SCHEMA public TO seshat_app;
    ALTER DEFAULT PRIVILEGES FOR ROLE agent IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO seshat_app;
"""
_COLUMNS_SQL = """
    SELECT column_name, data_type, is_nullable, column_default
    FROM information_schema.columns
    WHERE table_schema = $1 AND table_name = 'artifact_share_links'
    ORDER BY column_name
"""
_CHECKS_SQL = """
    SELECT pg_get_constraintdef(con.oid)
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid = rel.relnamespace
    WHERE ns.nspname = $1 AND rel.relname = 'artifact_share_links'
    ORDER BY 1
"""


def init_sql_statement() -> str:
    """Return the ``CREATE TABLE ... artifact_share_links`` statement from ``init.sql``."""
    text = (_DOCKER_DIR / "init.sql").read_text()
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS artifact_share_links \(.*?\n\);\n", text, flags=re.DOTALL
    )
    assert match is not None, "artifact_share_links CREATE TABLE not found in init.sql"
    return match.group(0)


@pytest_asyncio.fixture
async def scratch() -> AsyncIterator[asyncpg.Connection]:
    """Yield a connection, as ``agent``, to a scratch database that 0042 has run in."""
    admin_dsn = _normalize_asyncpg_dsn(settings.database_admin_url)
    name = f"fre1782_mig_{uuid4().hex[:8]}"
    try:
        admin = await asyncpg.connect(admin_dsn, timeout=5)
    except Exception as exc:  # noqa: BLE001 - environment guard
        pytest.skip(f"test-stack Postgres unavailable ({exc}); run `make test-infra-up`")
    roles = {
        r["rolname"]
        for r in await admin.fetch(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY($1)",
            ["agent", "seshat_app", "grafana_ro", "seshat_dev_broker"],
        )
    }
    if roles != {"agent", "seshat_app", "grafana_ro", "seshat_dev_broker"}:
        await admin.close()
        pytest.skip(f"test-stack Postgres lacks the init.sql roles: {sorted(roles)}")
    await admin.execute(f'CREATE DATABASE "{name}" OWNER agent')
    conn = await asyncpg.connect(admin_dsn.rsplit("/", 1)[0] + f"/{name}", timeout=5)
    try:
        await conn.execute("SET ROLE agent")
        await conn.execute(_DEFAULT_PRIVILEGES)
        await conn.execute(_PREREQUISITES)
        await conn.execute(_MIGRATION.read_text())
        yield conn
    finally:
        await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _shape(conn: asyncpg.Connection, schema: str) -> tuple[list[object], list[object]]:
    columns = [tuple(r.values()) for r in await conn.fetch(_COLUMNS_SQL, schema)]
    checks = [r[0] for r in await conn.fetch(_CHECKS_SQL, schema)]
    return columns, checks


async def test_migration_matches_init_sql(scratch: asyncpg.Connection) -> None:
    await scratch.execute("CREATE SCHEMA from_init")
    await scratch.execute("SET search_path TO from_init, public")
    await scratch.execute(init_sql_statement())
    await scratch.execute("SET search_path TO public")

    migrated = await _shape(scratch, "public")
    from_init = await _shape(scratch, "from_init")
    assert migrated == from_init
    assert len(migrated[0]) == 10
    assert any("30 days" in check for check in migrated[1])


async def test_migration_is_idempotent(scratch: asyncpg.Connection) -> None:
    before = await _shape(scratch, "public")
    await scratch.execute(_MIGRATION.read_text())
    assert await _shape(scratch, "public") == before


async def test_the_app_role_can_read_insert_and_update_but_not_delete(
    scratch: asyncpg.Connection,
) -> None:
    owner = await scratch.fetchval(
        "SELECT tableowner FROM pg_tables WHERE tablename = 'artifact_share_links'"
    )
    assert owner == "agent"
    privileges = {
        (role, privilege): await scratch.fetchval(
            "SELECT has_table_privilege($1, 'public.artifact_share_links', $2)", role, privilege
        )
        for role in ("seshat_app", "grafana_ro", "seshat_dev_broker")
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
    }
    assert {k for k, v in privileges.items() if v} == {
        ("seshat_app", "SELECT"),
        ("seshat_app", "INSERT"),
        ("seshat_app", "UPDATE"),
    }


async def test_the_app_role_really_writes_and_cannot_delete(scratch: asyncpg.Connection) -> None:
    user_id, artifact_id, link_id = uuid4(), uuid4(), uuid4()
    await scratch.execute("INSERT INTO users VALUES ($1, 'fre1782-mig@example.com')", user_id)
    await scratch.execute("INSERT INTO artifacts VALUES ($1, $2)", artifact_id, user_id)
    await scratch.execute("GRANT SELECT ON users, artifacts TO seshat_app")
    await scratch.execute("SET ROLE seshat_app")
    await scratch.execute(
        "INSERT INTO artifact_share_links (id, artifact_id, created_by, token_hash, expires_at)"
        " VALUES ($1, $2, $3, $4, now() + make_interval(days => 7))",
        link_id,
        artifact_id,
        user_id,
        b"\x01" * 32,
    )
    await scratch.execute(
        "UPDATE artifact_share_links SET view_count = view_count + 1 WHERE id = $1", link_id
    )
    assert await scratch.fetchval("SELECT view_count FROM artifact_share_links") == 1
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await scratch.execute("DELETE FROM artifact_share_links WHERE id = $1", link_id)
    await scratch.execute("RESET ROLE")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await scratch.execute("DELETE FROM artifacts WHERE id = $1", artifact_id)


async def test_the_table_has_no_address_or_client_column(scratch: asyncpg.Connection) -> None:
    rows = await scratch.fetch(_COLUMNS_SQL, "public")
    for row in rows:
        assert row["data_type"] not in {"inet", "cidr"}
        name = row["column_name"].lower()
        assert not any(part in name for part in ("ip", "agent", "referer", "referrer"))
