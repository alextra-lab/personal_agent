"""ADR-0088 D4 — the executor reports turn progress via the spine, not direct emit (FRE-513).

After FRE-513 the executor no longer calls ``emit_turn_status`` directly; it publishes a
best-effort ``turn.progress`` event that the projector relays. These tests pin that the
reporter publishes the right event and that the transport emitter is no longer imported by
the executor (the projector is the sole ``turn_status`` emitter).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import personal_agent.events as events_pkg
from personal_agent.events.models import TurnProgressEvent
from personal_agent.orchestrator import executor as executor_mod


@pytest.mark.asyncio
async def test_report_turn_progress_publishes_event(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = AsyncMock()
    monkeypatch.setattr(events_pkg, "get_event_bus", lambda: bus)

    ctx = SimpleNamespace(
        session_id=str(uuid4()),
        trace_id=str(uuid4()),
        tool_iteration_count=2,
        tool_iteration_bonus=0,
        gateway_output=None,
        messages=[],
        topology="hybrid_fanout",
    )
    await executor_mod._report_turn_progress(ctx)  # type: ignore[arg-type]

    bus.publish.assert_awaited_once()
    stream, event = bus.publish.await_args.args[0], bus.publish.await_args.args[1]
    assert stream == "stream:turn.observed"
    assert isinstance(event, TurnProgressEvent)
    assert event.trace_id == ctx.trace_id
    assert event.tool_iteration == 2
    assert event.topology == "hybrid_fanout"


@pytest.mark.asyncio
async def test_report_turn_progress_noop_without_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = AsyncMock()
    monkeypatch.setattr(events_pkg, "get_event_bus", lambda: bus)
    ctx = SimpleNamespace(session_id=None, trace_id=None, tool_iteration_count=0, messages=[])

    await executor_mod._report_turn_progress(ctx)  # type: ignore[arg-type]
    bus.publish.assert_not_awaited()


def test_executor_does_not_import_emit_turn_status() -> None:
    """The projector is the sole turn_status emitter — the executor must not call it."""
    assert not hasattr(executor_mod, "_emit_turn_status")
    assert not hasattr(executor_mod, "emit_turn_status")
