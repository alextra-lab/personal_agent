"""Integration test for :class:`JoinabilityWalk` against ``make test-infra-up``.

Seeds one healthy session+api_costs tuple in Postgres, runs the walk, and
asserts a green PG result. The red/yellow/skipped paths are exhaustively
covered by ``tests/observability/test_joinability_walk_unit.py``; this
integration test exists solely to prove the SQL queries match the live
schema and the substrate clients open/close cleanly.

Skipped gracefully when test Postgres (:5433) is unreachable, so the
suite still passes locally without ``make test-infra-up``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio

pytestmark = pytest.mark.integration

asyncpg = pytest.importorskip("asyncpg")


async def _try_open_pool() -> Any | None:
    from personal_agent.config.settings import get_settings

    settings = get_settings()
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        return await asyncpg.create_pool(dsn, min_size=1, max_size=2, timeout=5.0)
    except Exception:  # noqa: BLE001
        return None


SESSION_ID = uuid.uuid4()
TRACE_A = uuid.uuid4()
TRACE_B = uuid.uuid4()
# Fixed test user so the sessions.user_id FK (FRE-591) is satisfied.
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000591")


_BUDGET_ROLE = "joinability_walk_itest"


async def _seed(pool: Any) -> None:
    """Insert one known-good session with two api_costs rows + one budget_reservations row."""
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM budget_reservations WHERE role = $1", _BUDGET_ROLE)
        await conn.execute("DELETE FROM budget_counters WHERE role = $1", _BUDGET_ROLE)
        await conn.execute("DELETE FROM api_costs WHERE session_id = $1", SESSION_ID)
        await conn.execute("DELETE FROM sessions WHERE session_id = $1", SESSION_ID)
        # Seed the FK target first (sessions.user_id NOT NULL → users, FRE-591).
        await conn.execute(
            "INSERT INTO users (user_id, email) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            USER_ID,
            "joinability-walk@test.local",
        )
        await conn.execute(
            """
            INSERT INTO sessions (
                session_id, created_at, last_active_at, mode, channel,
                metadata, messages, user_id,
                primary_model_at_creation, model_config_path
            ) VALUES ($1, $2, $2, 'NORMAL', 'cli',
                      '{}'::jsonb, '[]'::jsonb, $3,
                      'test-model', 'config/models/test.yaml')
            """,
            SESSION_ID,
            datetime.now(timezone.utc) - timedelta(minutes=15),
            USER_ID,
        )
        for trace_id in (TRACE_A, TRACE_B):
            await conn.execute(
                """
                INSERT INTO api_costs (
                    timestamp, provider, model, input_tokens, output_tokens,
                    cost_usd, trace_id, session_id, purpose, latency_ms
                ) VALUES (NOW(), 'test', 'test-model', 10, 20, 0.0001,
                          $1, $2, 'user_request', 120)
                """,
                trace_id,
                SESSION_ID,
            )
        # FRE-693 (ADR-0074 §8c, AC-12): a cost-gate reservation with session_id
        # set must join back to the anchor session with zero orphans.
        counter_id = await conn.fetchval(
            """
            INSERT INTO budget_counters (
                user_id, time_window, provider, role, window_start, running_total
            ) VALUES (NULL, 'daily', NULL, $1, date_trunc('day', NOW()), 0)
            ON CONFLICT (user_id, time_window, provider, role, window_start)
            DO UPDATE SET role = EXCLUDED.role
            RETURNING id
            """,
            _BUDGET_ROLE,
        )
        await conn.execute(
            """
            INSERT INTO budget_reservations (
                counter_id, role, amount_usd, status,
                created_at, expires_at, trace_id, session_id, task_id
            ) VALUES ($1, $2, 0.01, 'committed', NOW(), NOW() + interval '90 seconds', $3, $4, NULL)
            """,
            counter_id,
            _BUDGET_ROLE,
            TRACE_A,
            SESSION_ID,
        )


async def _cleanup(pool: Any) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM budget_reservations WHERE role = $1", _BUDGET_ROLE)
        await conn.execute("DELETE FROM budget_counters WHERE role = $1", _BUDGET_ROLE)
        await conn.execute("DELETE FROM api_costs WHERE session_id = $1", SESSION_ID)
        await conn.execute("DELETE FROM sessions WHERE session_id = $1", SESSION_ID)


@pytest_asyncio.fixture
async def pool() -> Any:
    p = await _try_open_pool()
    if p is None:
        pytest.skip("Test Postgres (:5433) not reachable — run `make test-infra-up`.")
    try:
        await _seed(p)
        yield p
    finally:
        await _cleanup(p)
        await p.close()


@pytest.mark.asyncio
async def test_walk_returns_green_for_seeded_session(pool: Any) -> None:
    """Healthy seeded session → walk reports green across Postgres substrates."""
    from personal_agent.observability.joinability.walk import JoinabilityWalk
    from personal_agent.telemetry.trace import SystemTraceContext

    walk = JoinabilityWalk(
        pg_pool=pool,
        es=None,  # ES walks skip when client absent — still green overall
        neo4j_driver=None,
        redis=None,
        ctx=SystemTraceContext.new("joinability_probe_itest"),
        logs_prefix="agent-logs-test",
        captures_prefix="agent-captains-test",
    )
    doc = await walk.run(
        str(SESSION_ID),
        source="ci",
        window_hours=24,
        random_seed=0,
    )
    pg_checks = [c for c in doc.substrate_checks if c.substrate.startswith("postgres.")]
    bad = [c for c in pg_checks if c.status != "green"]
    assert not bad, f"non-green postgres checks: {bad}"
    assert doc.sampled_session_id == str(SESSION_ID)
    assert {str(TRACE_A), str(TRACE_B)} == set(doc.sampled_trace_ids)
