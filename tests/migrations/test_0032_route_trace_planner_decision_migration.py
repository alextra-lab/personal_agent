"""FRE-1512 (ADR-0154 D6) — migration 0032 idempotency and init.sql parity.

Two ephemeral schemas in the test-stack Postgres:

* schema A holds ``route_traces`` built from the ``CREATE TABLE`` statement in ``init.sql``;
* schema B holds ``route_traces`` built from the ordered migration chain
  (0009, 0010, 0031, 0032).

AC-1: applying 0032 twice raises no error, and the two schemas have the same columns,
types, lengths, nullability and defaults. Skips cleanly if the test stack is not running
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
_MIGRATIONS = _DOCKER_DIR / "migrations"
_CHAIN = (
    "0009_route_trace_ledger.sql",
    "0010_route_trace_topology_key.sql",
    "0031_route_trace_pause_instrumentation.sql",
    "0032_route_trace_planner_decision.sql",
)

NEW_COLUMNS = {
    "planner_decision",
    "planner_failure_reason",
    "planner_deployment",
    "planner_mode",
    "planner_reasoning_chars",
    "planner_duration_ms",
    "planner_prompt_tokens",
    "planner_completion_tokens",
    "planner_input_chars",
    "planner_gate_reason",
    "conversation_history_chars",
    "expansion_budget",
    "synthesis_appended",
    "first_token_ms",
}

_COLUMNS_SQL = """
    SELECT column_name, data_type, udt_name, character_maximum_length,
           numeric_precision, numeric_scale, is_nullable, column_default
    FROM information_schema.columns
    WHERE table_schema = $1 AND table_name = 'route_traces'
    ORDER BY column_name
"""


def _init_sql_route_traces_statement() -> str:
    """Return the ``CREATE TABLE ... route_traces`` statement from ``init.sql``."""
    text = (_DOCKER_DIR / "init.sql").read_text()
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS route_traces \(.*?\n\);\n", text, flags=re.DOTALL
    )
    assert match is not None, "route_traces CREATE TABLE not found in init.sql"
    return match.group(0)


@pytest_asyncio.fixture
async def two_schemas():
    """Yield ``(conn, schema_a, schema_b)``; drop both at teardown."""
    dsn = _normalize_asyncpg_dsn(settings.database_admin_url)
    suffix = uuid4().hex[:8]
    schema_a, schema_b = f"mig1512_init_{suffix}", f"mig1512_chain_{suffix}"
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
    await conn.execute(f"SET search_path TO {schema}")
    return [tuple(r.values()) for r in await conn.fetch(_COLUMNS_SQL, schema)]


@pytest.mark.asyncio
async def test_migration_chain_matches_init_sql(two_schemas) -> None:
    """The migration chain and ``init.sql`` build the same ``route_traces`` columns."""
    conn, schema_a, schema_b = two_schemas
    await conn.execute(f"SET search_path TO {schema_a}")
    await conn.execute(_init_sql_route_traces_statement())
    await conn.execute(f"SET search_path TO {schema_b}")
    for name in _CHAIN:
        await conn.execute((_MIGRATIONS / name).read_text())

    from_init = await _columns(conn, schema_a)
    from_chain = await _columns(conn, schema_b)
    assert from_init == from_chain
    assert NEW_COLUMNS <= {row[0] for row in from_chain}


@pytest.mark.asyncio
async def test_migration_0032_is_idempotent(two_schemas) -> None:
    """Applying 0032 a second time raises no error and changes nothing."""
    conn, _schema_a, schema_b = two_schemas
    await conn.execute(f"SET search_path TO {schema_b}")
    for name in _CHAIN:
        await conn.execute((_MIGRATIONS / name).read_text())
    before = await _columns(conn, schema_b)

    await conn.execute(f"SET search_path TO {schema_b}")
    await conn.execute((_MIGRATIONS / _CHAIN[-1]).read_text())
    assert await _columns(conn, schema_b) == before
