"""ADR-0088 D3 — record_api_call publishes a best-effort live cost event (FRE-513).

The cost boundary is the hard-enforced choke point every model call passes through, so it
emits ``turn.model_call_completed`` to drive the projector's topology-independent live
meter. The publish is live-only: it happens after the durable ``api_costs`` write and a
bus failure must never break cost recording.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import personal_agent.events as events_pkg
from personal_agent.events.models import ModelCallCompletedEvent
from personal_agent.llm_client.cost_tracker import CostTrackerService


def _tracker_with_mock_pool() -> CostTrackerService:
    tracker = CostTrackerService()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=7)
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    tracker.pool = pool  # type: ignore[assignment]
    return tracker


@pytest.mark.asyncio
async def test_record_api_call_publishes_model_call_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = AsyncMock()
    monkeypatch.setattr(events_pkg, "get_event_bus", lambda: bus)
    tracker = _tracker_with_mock_pool()
    trace_id = uuid4()
    session_id = uuid4()

    record_id = await tracker.record_api_call(
        provider="anthropic",
        model="claude-opus-4.8",
        input_tokens=1200,
        output_tokens=800,
        cost_usd=0.42,
        trace_id=trace_id,
        session_id=session_id,
        purpose="sub_agent",
    )

    assert record_id == 7
    bus.publish.assert_awaited_once()
    stream, event = bus.publish.await_args.args[0], bus.publish.await_args.args[1]
    assert stream == "stream:turn.observed"
    assert isinstance(event, ModelCallCompletedEvent)
    assert event.trace_id == str(trace_id)
    assert event.session_id == str(session_id)
    assert event.cost_usd == pytest.approx(0.42)
    assert event.input_tokens == 1200
    assert event.output_tokens == 800
    assert event.model_role == "sub_agent"


@pytest.mark.asyncio
async def test_publish_failure_does_not_break_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = AsyncMock()
    bus.publish = AsyncMock(side_effect=RuntimeError("redis down"))
    monkeypatch.setattr(events_pkg, "get_event_bus", lambda: bus)
    tracker = _tracker_with_mock_pool()

    # A failing bus must be swallowed — the durable record id is still returned.
    record_id = await tracker.record_api_call(
        provider="openai",
        model="gpt-5.4-mini",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.001,
        trace_id=uuid4(),
        session_id=uuid4(),
    )
    assert record_id == 7
