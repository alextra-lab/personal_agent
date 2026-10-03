"""Tests for the route-trace ledger service (FRE-452).

Unit tests cover the ADR-0074 identity guard and the INSERT shape (idempotent
``ON CONFLICT``) with a mocked asyncpg connection. A marked integration test exercises a
real write → read round-trip against the isolated test substrate (FRE-375); it is not run
in agent sessions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from personal_agent.exceptions import MissingIdentityError
from personal_agent.observability.route_trace.ledger import RouteTraceLedger
from personal_agent.observability.route_trace.types import RouteTraceRow

pytestmark = pytest.mark.asyncio

# Idempotent route_traces schema chain applied by the integration tests (FRE-1512).
_MIGRATION_CHAIN = (
    "0009_route_trace_ledger.sql",
    "0010_route_trace_topology_key.sql",
    "0031_route_trace_pause_instrumentation.sql",
    "0032_route_trace_planner_decision.sql",
)


def _migrations_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "docker" / "postgres" / "migrations"


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
    # 57 bound parameters follow the SQL string (43 + the 14 ADR-0154 D6 columns, FRE-1512).
    assert len(conn.execute.call_args.args) == 58
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
        effective_tool_iteration_ceiling=None,
        constraint_resolutions=None,
        planner_decision=None,
        planner_failure_reason=None,
        planner_deployment=None,
        planner_mode=None,
        planner_reasoning_chars=None,
        planner_duration_ms=None,
        planner_prompt_tokens=None,
        planner_completion_tokens=None,
        planner_input_chars=None,
        planner_gate_reason=None,
        conversation_history_chars=None,
        expansion_budget=None,
        synthesis_appended=None,
        first_token_ms=None,
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


# ---------------------------------------------------------------------------
# FRE-1512 — ADR-0154 D6: planner decision, inputs, delay and first token
# ---------------------------------------------------------------------------


async def test_row_from_record_maps_planner_fields() -> None:
    """Every ADR-0154 D6 column round-trips from a record into the DTO."""
    ledger = RouteTraceLedger()
    pool = MagicMock()
    pool.fetch = AsyncMock(
        return_value=[
            _record(
                planner_decision="failed",
                planner_failure_reason="input_too_large",
                planner_deployment="qwen-local",
                planner_mode="planner",
                planner_reasoning_chars=0,
                planner_duration_ms=812.5,
                planner_prompt_tokens=900,
                planner_completion_tokens=40,
                planner_input_chars='{"system": 2439, "history": 61000, "digest": 0, "message": 10}',
                planner_gate_reason=None,
                conversation_history_chars=61000,
                expansion_budget=3,
                synthesis_appended=False,
                first_token_ms=1534.0,
            )
        ]
    )
    ledger.pool = pool

    (row,) = await ledger.get_by_trace_id(uuid4())

    assert row.planner_decision == "failed"
    assert row.planner_failure_reason == "input_too_large"
    assert row.planner_deployment == "qwen-local"
    assert row.planner_mode == "planner"
    assert row.planner_reasoning_chars == 0
    assert row.planner_duration_ms == pytest.approx(812.5)
    assert row.planner_prompt_tokens == 900
    assert row.planner_completion_tokens == 40
    assert row.planner_input_chars == {"system": 2439, "history": 61000, "digest": 0, "message": 10}
    assert row.conversation_history_chars == 61000
    assert row.expansion_budget == 3
    assert row.synthesis_appended is False
    assert row.first_token_ms == pytest.approx(1534.0)


async def test_row_from_record_planner_fields_default_to_none() -> None:
    """A turn where the planner did not run carries NULL planner fields."""
    ledger = RouteTraceLedger()
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[_record()])
    ledger.pool = pool

    (row,) = await ledger.get_by_trace_id(uuid4())

    assert row.planner_decision is None
    assert row.planner_input_chars is None
    assert row.first_token_ms is None


async def test_write_binds_planner_fields() -> None:
    ledger = RouteTraceLedger()
    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCM(conn))
    ledger.pool = pool

    await ledger.write(
        _row(
            planner_decision="declined",
            planner_input_chars={"system": 1, "history": 2, "digest": 3, "message": 4},
            first_token_ms=7.5,
        )
    )

    args = conn.execute.call_args.args
    assert "declined" in args
    assert '{"system": 1, "history": 2, "digest": 3, "message": 4}' in args
    assert 7.5 in args
    sql = args[0]
    for column in ("planner_decision", "planner_input_chars", "first_token_ms", "expansion_budget"):
        assert column in sql


async def test_label_lie_predicate_excludes_declined_and_failed_planner_asked() -> None:
    """ADR-0154 D6 / AC-4: a decline or a failure is not a lying label."""
    ledger = RouteTraceLedger()
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    ledger.pool = pool

    await ledger.list_recent(label_lie=True)

    sql = pool.fetch.call_args.args[0]
    assert "planner_decision" in sql
    assert "planner_asked" in sql


async def test_set_first_token_ms_updates_turn_row_once() -> None:
    ledger = RouteTraceLedger()
    pool = MagicMock()
    tid = uuid4()
    pool.fetchrow = AsyncMock(return_value=_record(trace_id=tid, first_token_ms=1200.0))
    ledger.pool = pool

    row = await ledger.set_first_token_ms(tid, 1200.0)

    sql = pool.fetchrow.call_args.args[0]
    assert "UPDATE route_traces SET first_token_ms = $2" in sql
    assert "task_id IS NULL" in sql  # turn-level row only
    assert "first_token_ms IS NULL" in sql  # first write wins
    assert pool.fetchrow.call_args.args[1:] == (tid, 1200.0)
    assert row is not None and row.first_token_ms == pytest.approx(1200.0)


async def test_set_first_token_ms_returns_none_when_no_row() -> None:
    ledger = RouteTraceLedger()
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    ledger.pool = pool

    assert await ledger.set_first_token_ms(uuid4(), 5.0) is None


async def test_set_first_token_ms_none_when_unconnected() -> None:
    ledger = RouteTraceLedger()
    ledger.pool = None
    assert await ledger.set_first_token_ms(uuid4(), 5.0) is None


# ---------------------------------------------------------------------------
# FRE-568 — fetch_session_costs_by_trace (ADR-0092 §D2/§D4 hydration read)
# ---------------------------------------------------------------------------


async def test_fetch_session_costs_by_trace_groups_by_trace_id() -> None:
    """Returns a {trace_id_str: cost} map grouped by trace_id, not a bare SUM."""
    ledger = RouteTraceLedger()
    pool = MagicMock()

    tid1 = uuid4()
    tid2 = uuid4()
    pool.fetch = AsyncMock(
        return_value=[
            {"trace_id": tid1, "cost": 0.5},
            {"trace_id": tid2, "cost": 0.3},
        ]
    )
    ledger.pool = pool

    result = await ledger.fetch_session_costs_by_trace(str(uuid4()))

    pool.fetch.assert_awaited_once()
    sql = pool.fetch.call_args.args[0]
    assert "GROUP BY trace_id" in sql
    assert "session_id = $1" in sql
    assert result == {str(tid1): pytest.approx(0.5), str(tid2): pytest.approx(0.3)}


async def test_fetch_session_costs_by_trace_empty_when_unconnected() -> None:
    ledger = RouteTraceLedger()
    ledger.pool = None
    assert await ledger.fetch_session_costs_by_trace(str(uuid4())) == {}


@pytest.mark.integration
async def test_write_read_roundtrip_and_idempotency() -> None:
    """Real round-trip against the isolated test substrate (requires make test-infra-up)."""
    ledger = await _provisioned_ledger()
    try:
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


async def _provisioned_ledger() -> RouteTraceLedger:
    """Apply the route_traces migration chain as admin, then connect a ledger as the app role.

    The ledger's own pool is the restricted ``seshat_app`` role, which cannot run DDL
    (FRE-808), so the idempotent migrations go through ``database_admin_url``.
    """
    import asyncpg

    from personal_agent.config import settings
    from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn

    migrations_dir = _migrations_dir()
    try:
        admin = await asyncpg.connect(
            _normalize_asyncpg_dsn(settings.database_admin_url), timeout=5
        )
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"route-trace test substrate unavailable ({exc})")
    try:
        for name in _MIGRATION_CHAIN:
            await admin.execute((migrations_dir / name).read_text())
    finally:
        await admin.close()

    ledger = RouteTraceLedger()
    await ledger.connect()
    if ledger.pool is None:
        pytest.skip("route-trace test substrate unavailable")
    return ledger


@pytest.mark.integration
async def test_first_token_update_after_real_insert() -> None:
    """FRE-1512 AC-2 lifecycle: the seam insert lands first, the update fills first_token_ms."""
    ledger = await _provisioned_ledger()
    try:
        trace_id = uuid4()
        await ledger.write(_row(trace_id=trace_id, conversation_history_chars=120))

        updated = await ledger.set_first_token_ms(trace_id, 1534.5)
        assert updated is not None
        assert updated.first_token_ms == pytest.approx(1534.5)
        assert updated.conversation_history_chars == 120

        # First write wins: a second update is a no-op and returns no row.
        assert await ledger.set_first_token_ms(trace_id, 9999.0) is None
        (fetched,) = await ledger.get_by_trace_id(trace_id)
        assert fetched.first_token_ms == pytest.approx(1534.5)

        # No turn row (the seam write failed or never ran): nothing to update.
        assert await ledger.set_first_token_ms(uuid4(), 1.0) is None
    finally:
        await ledger.disconnect()


@pytest.mark.integration
async def test_label_lie_excludes_declined_and_failed_planner_rows() -> None:
    """FRE-1512 AC-4: seeded planner_asked rows; only the decision-less control still matches."""
    ledger = await _provisioned_ledger()
    try:
        sess = uuid4()
        seeded: dict[str, UUID] = {}
        for label, decision in (
            ("declined", "declined"),
            ("failed", "failed"),
            ("expanded", "expanded"),
            ("control", None),
        ):
            tid = uuid4()
            seeded[label] = tid
            await ledger.write(
                _row(
                    trace_id=tid,
                    session_id=sess,
                    task_type="conversational",
                    decomposition_strategy="hybrid",
                    decomposition_reason="planner_asked",
                    orchestration_event="primary_handled",
                    gateway_label="conversational/hybrid",
                    planner_decision=decision,
                )
            )
        listed = {r.trace_id for r in await ledger.list_recent(label_lie=True, limit=500)}

        # Seeded negative: the old behaviour (no decision) still reads as a candidate.
        assert seeded["control"] in listed
        assert seeded["declined"] not in listed
        assert seeded["failed"] not in listed
        # An expanded row that ended primary_handled is still a genuine label-lie candidate.
        assert seeded["expanded"] in listed
    finally:
        await ledger.disconnect()
