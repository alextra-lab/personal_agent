"""Tests for the route-trace ledger service (FRE-452).

Unit tests cover the ADR-0074 identity guard and the INSERT shape (idempotent
``ON CONFLICT``) with a mocked asyncpg connection. A marked integration test exercises a
real write → read round-trip against the isolated test substrate (FRE-375); it is not run
in agent sessions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from personal_agent.exceptions import MissingIdentityError
from personal_agent.observability.route_trace.ledger import RouteTraceLedger
from personal_agent.observability.route_trace.types import RouteTraceRow

pytestmark = pytest.mark.asyncio


def _row(**overrides: object) -> RouteTraceRow:
    """Build a minimally-valid route-trace row for write tests."""
    base: dict[str, object] = dict(
        trace_id=uuid4(),
        session_id=uuid4(),
        created_at=datetime.now(timezone.utc),
        orchestration_event="primary_handled",
        gateway_label="memory_recall/single",
    )
    base.update(overrides)
    return RouteTraceRow(**base)  # type: ignore[arg-type]


class _AcquireCM:
    """Minimal async-context-manager stand-in for ``pool.acquire()``."""

    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def test_write_raises_without_trace_id() -> None:
    ledger = RouteTraceLedger()
    ledger.pool = MagicMock()  # guard must fire before any pool use
    with pytest.raises(MissingIdentityError):
        await ledger.write(_row(trace_id=None))


async def test_write_raises_without_session_id() -> None:
    ledger = RouteTraceLedger()
    ledger.pool = MagicMock()
    with pytest.raises(MissingIdentityError):
        await ledger.write(_row(session_id=None))


async def test_write_issues_idempotent_insert() -> None:
    ledger = RouteTraceLedger()
    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCM(conn))
    ledger.pool = pool

    row = _row()
    await ledger.write(row)

    conn.execute.assert_awaited_once()
    sql = conn.execute.call_args.args[0]
    assert "INSERT INTO route_traces" in sql
    assert "ON CONFLICT (trace_id) DO NOTHING" in sql
    # 41 bound parameters follow the SQL string.
    assert len(conn.execute.call_args.args) == 42
    # Identity params are passed first, as UUIDs.
    assert conn.execute.call_args.args[1] == row.trace_id
    assert conn.execute.call_args.args[2] == row.session_id


async def test_write_noop_when_not_connected() -> None:
    ledger = RouteTraceLedger()
    ledger.pool = None
    # Valid identity, but no pool → logs and returns without raising.
    await ledger.write(_row())


async def test_fetch_authoritative_cost_sums_api_costs() -> None:
    ledger = RouteTraceLedger()
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"cost": 0.9028, "in_tok": 1200, "out_tok": 800})
    ledger.pool = pool

    cost, in_tok, out_tok = await ledger.fetch_authoritative_cost(uuid4())
    assert cost == pytest.approx(0.9028)
    assert in_tok == 1200
    assert out_tok == 800


async def test_fetch_authoritative_cost_zero_when_unconnected() -> None:
    ledger = RouteTraceLedger()
    ledger.pool = None
    assert await ledger.fetch_authoritative_cost(uuid4()) == (0.0, 0, 0)


@pytest.mark.integration
async def test_write_read_roundtrip_and_idempotency() -> None:
    """Real round-trip against the isolated test substrate (requires make test-infra-up)."""
    from pathlib import Path

    ledger = RouteTraceLedger()
    await ledger.connect()
    if ledger.pool is None:
        pytest.skip("route-trace test substrate unavailable")
    try:
        # Self-provision the schema from the canonical migration (idempotent).
        migration = (
            Path(__file__).resolve().parents[3]
            / "docker"
            / "postgres"
            / "migrations"
            / "0009_route_trace_ledger.sql"
        )
        await ledger.pool.execute(migration.read_text())

        trace_id = uuid4()
        row = _row(
            trace_id=trace_id,
            task_type="memory_recall",
            decomposition_strategy="single",
            degraded_stages=("context",),
            tools_used=("web_search",),
            routing_history=({"decision": "HANDLE"},),
            sub_agents=({"task_id": "s1", "success": True},),
            latency_breakdown={"total_duration_ms": 12.0},
            cost_authoritative_usd=0.5,
        )
        await ledger.write(row)
        await ledger.write(row)  # second write must be a no-op (ON CONFLICT)

        fetched = await ledger.get_by_trace_id(trace_id)
        assert fetched is not None
        assert fetched.trace_id == trace_id
        assert fetched.task_type == "memory_recall"
        assert fetched.gateway_label == "memory_recall/single"
        assert fetched.degraded_stages == ("context",)
        assert fetched.routing_history == ({"decision": "HANDLE"},)
        assert fetched.latency_breakdown == {"total_duration_ms": 12.0}
    finally:
        await ledger.disconnect()
