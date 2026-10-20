"""FRE-1622 (ADR-0157 D8): every turn's route-trace row and ES document carry its level (AC-6)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from personal_agent.config.seshat_effort import (
    EFFORT_LEVELS,
    effort_spec,
    reset_current_effort,
    set_current_effort,
)
from personal_agent.observability.route_trace.assembler import assemble_route_trace
from personal_agent.observability.route_trace.ledger import RouteTraceLedger, _row_from_record
from personal_agent.observability.topology.es_projection import build_topology_doc
from tests.observability.route_trace.test_ledger import (
    _AcquireCM,
    _provisioned_ledger,
    _record,
    _row,
)

pytestmark = pytest.mark.asyncio


def _ctx_for(level: str, mode: str) -> object:
    """An ExecutionContext built inside a turn of ``level``, as the chat path builds it."""
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext

    token = set_current_effort(effort_spec(level))  # type: ignore[arg-type]
    try:
        ctx = ExecutionContext(
            session_id=str(uuid4()),
            trace_id=str(uuid4()),
            user_message="hi",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )
    finally:
        reset_current_effort(token)
    ctx.primary_mode = mode
    return ctx


@pytest.mark.parametrize("level", EFFORT_LEVELS)
async def test_the_row_and_its_es_document_carry_the_level_and_mode(level: str) -> None:
    mode = "fast" if level == "fast" else "default"
    row = assemble_route_trace(
        _ctx_for(level, mode),
        authoritative_cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        store_preview=False,
        preview_chars=0,
        created_at=datetime(2026, 10, 20, tzinfo=UTC),
    )
    assert (row.seshat_effort, row.primary_mode) == (level, mode)
    assert row.schema_version == 4
    doc = build_topology_doc(row, topology="primary")
    assert (doc["seshat_effort"], doc["primary_mode"]) == (level, mode)


async def test_outside_a_chat_turn_the_level_is_absent() -> None:
    row = assemble_route_trace(
        SimpleNamespace(trace_id=str(uuid4()), session_id=None, messages=[], steps=[]),
        authoritative_cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        store_preview=False,
        preview_chars=0,
    )
    assert row.seshat_effort is None and row.primary_mode is None
    assert "seshat_effort" not in build_topology_doc(row, topology="primary")


async def test_the_ledger_binds_both_columns_last() -> None:
    ledger = RouteTraceLedger()
    conn = AsyncMock()
    ledger.pool = MagicMock(acquire=MagicMock(return_value=_AcquireCM(conn)))

    await ledger.write(_row(seshat_effort="deep", primary_mode="default"))

    args = conn.execute.call_args.args
    assert "seshat_effort, primary_mode" in args[0] and "$58, $59" in args[0]
    assert args[-2:] == ("deep", "default")


async def test_the_read_back_maps_both_columns() -> None:
    row = _row_from_record(_record(seshat_effort="fast", primary_mode="fast"))
    assert (row.seshat_effort, row.primary_mode) == ("fast", "fast")


@pytest.mark.integration
async def test_a_real_round_trip_keeps_both_columns() -> None:
    """Against the isolated test substrate (requires make test-infra-up)."""
    ledger = await _provisioned_ledger()
    try:
        trace_id = uuid4()
        await ledger.write(_row(trace_id=trace_id, seshat_effort="standard", primary_mode="default"))
        (fetched,) = await ledger.get_by_trace_id(trace_id)
        assert (fetched.seshat_effort, fetched.primary_mode) == ("standard", "default")
    finally:
        await ledger.disconnect()
