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
    # ADR-0088 seam key: per-topology rows are keyed by (trace_id, task_id), so the
    # idempotency conflict target migrated from (trace_id) to (trace_id, task_id).
    assert "ON CONFLICT (trace_id, task_id) DO NOTHING" in sql
    # 41 bound parameters follow the SQL string.
    assert len(conn.execute.call_args.args) == 42
    # Identity params are passed first, as UUIDs; task_id is the third bound param.
    assert conn.execute.call_args.args[1] == row.trace_id
    assert conn.execute.call_args.args[2] == row.session_id
    assert conn.execute.call_args.args[3] == row.task_id


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


def _record(**overrides: object) -> dict[str, object]:
    """Build a complete ``route_traces`` record dict for ``_row_from_record`` (FRE-514).

    Mirrors every column the reader touches so the round-trip mapping succeeds with a
    plain dict standing in for an ``asyncpg.Record``.
    """
    base: dict[str, object] = dict(
        trace_id=uuid4(),
        session_id=uuid4(),
        task_id=None,
        created_at=datetime.now(timezone.utc),
        schema_version=1,
        user_message_chars=10,
        message_count=2,
        user_message_sha256="abc123",
        user_message_preview=None,
        task_type="memory_recall",
        complexity="simple",
        intent_confidence=0.9,
        decomposition_strategy="single",
        decomposition_reason="memory_recall_always_single",
        degraded_stages=None,
        mode="standard",
        channel="chat",
        gateway_label="memory_recall/single",
        model_role="primary",
        thinking_enabled=None,
        routing_history=None,
        tool_iteration_count=0,
        tools_used=None,
        skills_loaded=None,
        sub_agent_count=0,
        sub_agents=None,
        expansion_strategy=None,
        delegate_result_passed_to_synthesis=False,
        orchestration_event="primary_handled",
        pedagogical_outcomes=None,
        final_reply_chars=42,
        latency_total_ms=12.0,
        latency_breakdown=None,
        cost_live_usd=0.5,
        cost_authoritative_usd=0.5,
        cost_reconciled=True,
        input_tokens=100,
        output_tokens=50,
        fallback_triggered=False,
        error_type=None,
        error_class=None,
    )
    base.update(overrides)
    return base


async def test_list_by_session_id_orders_desc_and_binds() -> None:
    ledger = RouteTraceLedger()
    pool = MagicMock()
    sid = uuid4()
    rec = _record(session_id=sid)
    pool.fetch = AsyncMock(return_value=[rec])
    ledger.pool = pool

    rows = await ledger.list_by_session_id(sid, limit=25)

    pool.fetch.assert_awaited_once()
    sql = pool.fetch.call_args.args[0]
    assert "WHERE session_id = $1" in sql
    # FRE-517: turn-level only — segment rows (task_id set) are excluded from the session view.
    assert "task_id IS NULL" in sql
    assert "ORDER BY created_at DESC" in sql
    assert "LIMIT $2" in sql
    assert pool.fetch.call_args.args[1] == sid
    assert pool.fetch.call_args.args[2] == 25
    assert len(rows) == 1
    assert rows[0].session_id == sid


async def test_list_by_session_empty_when_unconnected() -> None:
    ledger = RouteTraceLedger()
    ledger.pool = None
    assert await ledger.list_by_session_id(uuid4()) == []


async def test_list_recent_no_filters_sql() -> None:
    ledger = RouteTraceLedger()
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[_record(), _record()])
    ledger.pool = pool

    rows = await ledger.list_recent(limit=10)

    sql = pool.fetch.call_args.args[0]
    # FRE-517: the recent dashboard is turn-level only, so task_id IS NULL is always present.
    assert "WHERE task_id IS NULL" in sql
    assert "ORDER BY created_at DESC" in sql
    assert "LIMIT $1" in sql
    assert pool.fetch.call_args.args[1] == 10
    assert len(rows) == 2


async def test_list_recent_label_lie_predicate() -> None:
    ledger = RouteTraceLedger()
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    ledger.pool = pool

    await ledger.list_recent(label_lie=True)

    sql = pool.fetch.call_args.args[0]
    assert "WHERE" in sql
    assert "decomposition_strategy <> 'single'" in sql
    assert "orchestration_event = 'primary_handled'" in sql
    assert "orchestration_event IN" in sql


async def test_list_recent_combines_filters_with_and() -> None:
    ledger = RouteTraceLedger()
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    ledger.pool = pool

    await ledger.list_recent(fallback_triggered=True, not_reconciled=True)

    sql = pool.fetch.call_args.args[0]
    assert "fallback_triggered = TRUE" in sql
    assert "cost_reconciled = FALSE" in sql
    assert " AND " in sql


async def test_list_recent_empty_when_unconnected() -> None:
    ledger = RouteTraceLedger()
    ledger.pool = None
    assert await ledger.list_recent() == []


async def test_get_by_trace_id_returns_all_rows_for_trace() -> None:
    """FRE-517: get_by_trace_id returns the turn-level row + every segment row."""
    ledger = RouteTraceLedger()
    pool = MagicMock()
    tid = uuid4()
    seg_task = uuid4()
    pool.fetch = AsyncMock(
        return_value=[_record(trace_id=tid, task_id=None), _record(trace_id=tid, task_id=seg_task)]
    )
    ledger.pool = pool

    rows = await ledger.get_by_trace_id(tid)

    pool.fetch.assert_awaited_once()
    sql = pool.fetch.call_args.args[0]
    assert "WHERE trace_id = $1" in sql
    # Turn-level row first, segments chronological (not UUID-lexical).
    assert "ORDER BY (task_id IS NOT NULL), created_at ASC" in sql
    assert pool.fetch.call_args.args[1] == tid
    assert [r.task_id for r in rows] == [None, seg_task]


async def test_get_by_trace_id_empty_when_unconnected() -> None:
    ledger = RouteTraceLedger()
    ledger.pool = None
    assert await ledger.get_by_trace_id(uuid4()) == []


@pytest.mark.integration
async def test_write_read_roundtrip_and_idempotency() -> None:
    """Real round-trip against the isolated test substrate (requires make test-infra-up)."""
    from pathlib import Path

    ledger = RouteTraceLedger()
    await ledger.connect()
    if ledger.pool is None:
        pytest.skip("route-trace test substrate unavailable")
    try:
        # Self-provision the schema from the canonical migrations (idempotent): the
        # FRE-452 base table, then the ADR-0088 seam key migration (0010).
        migrations_dir = Path(__file__).resolve().parents[3] / "docker" / "postgres" / "migrations"
        await ledger.pool.execute((migrations_dir / "0009_route_trace_ledger.sql").read_text())
        await ledger.pool.execute(
            (migrations_dir / "0010_route_trace_topology_key.sql").read_text()
        )

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

        fetched_rows = await ledger.get_by_trace_id(trace_id)
        assert len(fetched_rows) == 1  # turn-level row only (no segments for this trace)
        fetched = fetched_rows[0]
        assert fetched.trace_id == trace_id
        assert fetched.task_id is None  # turn-level row sorts first
        assert fetched.task_type == "memory_recall"
        assert fetched.gateway_label == "memory_recall/single"
        assert fetched.degraded_stages == ("context",)
        assert fetched.routing_history == ({"decision": "HANDLE"},)
        assert fetched.latency_breakdown == {"total_duration_ms": 12.0}

        # list-by-session returns the row; recent + label_lie filter excludes this
        # honest single/primary_handled row.
        by_session = await ledger.list_by_session_id(row.session_id)  # type: ignore[arg-type]
        assert any(r.trace_id == trace_id for r in by_session)
        liars = await ledger.list_recent(label_lie=True, limit=200)
        assert all(r.trace_id != trace_id for r in liars)

        # ADR-0088 seam key (FRE-513/FRE-517): two rows sharing trace_id but with distinct
        # task_id are both persisted — the per-topology fan-out. A NULL task_id row is the
        # turn-level write; a non-NULL task_id is a topology segment.
        seam_trace = uuid4()
        sess = uuid4()
        await ledger.write(_row(trace_id=seam_trace, session_id=sess, task_id=None))
        sub_task = uuid4()
        await ledger.write(_row(trace_id=seam_trace, session_id=sess, task_id=sub_task))
        # FRE-517: get_by_trace_id returns BOTH rows (turn-level first), session view only the
        # turn-level row (segments excluded).
        trace_rows = await ledger.get_by_trace_id(seam_trace)
        assert len(trace_rows) == 2
        assert trace_rows[0].task_id is None and trace_rows[1].task_id == sub_task
        by_sess = await ledger.list_by_session_id(sess, limit=10)
        assert [r.task_id for r in by_sess if r.trace_id == seam_trace] == [None]
        # Re-writing the same (trace_id, task_id) is idempotent (incl. NULLS NOT DISTINCT
        # for the turn-level NULL slot).
        await ledger.write(_row(trace_id=seam_trace, session_id=sess, task_id=None))
        await ledger.write(_row(trace_id=seam_trace, session_id=sess, task_id=sub_task))
        rows_after = await ledger.get_by_trace_id(seam_trace)
        assert len(rows_after) == 2
    finally:
        await ledger.disconnect()
