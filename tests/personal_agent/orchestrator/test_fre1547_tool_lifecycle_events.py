"""FRE-1547 AC-3 — the executor streams each tool call, so the stored history carries it.

ADR-0123 §2 derives the tool rows of the live panel from ``ToolStartEvent`` /
``ToolEndEvent``, but no production code emitted them: the live panel and the stored
``turn_summary.tools`` were both empty. FRE-1543's summary test passed only because it fed
hand-made ``TOOL_CALL_START`` envelopes. These tests drive the real ``step_tool_execution``
through the real transport (persistence faked) and compare what the live socket receives with
what the turn's recorder stores.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import personal_agent.orchestrator.executor as ex
import personal_agent.transport.agui.transport as transport_mod
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from personal_agent.telemetry.trace import TraceContext
from personal_agent.transport.agui.ws_endpoint import get_event_queue
from personal_agent.transport.turn_summary import (
    build_turn_summary,
    start_turn_recording,
    stop_turn_recording,
)


class _SeqBuffer:
    """``SessionEventBuffer`` stand-in: assigns an ascending seq, stores nothing."""

    counter = 0

    def __init__(self, db: Any) -> None:
        """Accept and ignore the db session (matches the real API)."""

    async def append(self, session_id: Any, event_type: str, payload: dict[str, Any]) -> int:
        """Return the next seq."""
        type(self).counter += 1
        return type(self).counter


@asynccontextmanager
async def _fake_session() -> Any:
    yield None


def _assistant_calling(*names: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": f"tc-{name}", "function": {"name": name, "arguments": json.dumps({"q": name})}}
            for name in names
        ],
    }


def _ok_result(tool_call_id: str, tool_name: str, **rest: Any) -> dict[str, Any]:
    return {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "content": json.dumps({"status": "ok"}),
        "success": True,
        "latency_ms": 3.0,
        "output_hash": None,
        "gate_result": rest.get("gate_result"),
        "args_hash": rest.get("args_hash", ""),
        "loop_policy": rest.get("loop_policy"),
        "tool_layer_output": {"status": "ok"},
        "tool_layer_error": None,
        "terminal": False,
        "terminal_reason": None,
        "terminal_next_step": None,
    }


async def _fake_dispatch(*, tool_call_id: str, tool_name: str, **rest: Any) -> dict[str, Any]:
    return _ok_result(tool_call_id, tool_name, **rest)


@pytest.fixture
def session_id(monkeypatch: pytest.MonkeyPatch) -> str:
    """A session with faked persistence and the executor's other seams stubbed."""
    _SeqBuffer.counter = 0
    monkeypatch.setattr(transport_mod, "SessionEventBuffer", _SeqBuffer)
    monkeypatch.setattr(transport_mod, "AsyncSessionLocal", lambda: _fake_session())
    monkeypatch.setattr(ex, "_get_tool_execution_layer", lambda: object())
    monkeypatch.setattr(ex, "_is_turn_cancelled", lambda _sid: False)

    async def _noop_progress(_ctx: Any) -> None:
        return None

    monkeypatch.setattr(ex, "_report_turn_progress", _noop_progress)
    return str(uuid4())


def _ctx(session_id: str | None) -> ExecutionContext:
    return ExecutionContext(  # type: ignore[arg-type]
        session_id=session_id,
        trace_id="trace-fre1547",
        user_message="look it up",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )


def _drain(queue: asyncio.Queue[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def _names(envelopes: list[dict[str, Any]], kind: str) -> list[str]:
    return [e["data"]["tool_name"] for e in envelopes if e.get("type") == kind]


@pytest.mark.asyncio
async def test_two_tool_rounds_reach_the_live_panel_and_the_stored_history(
    session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: a turn with 2 tool rounds stores both tool calls, as the live panel shows them."""
    monkeypatch.setattr(ex, "dispatch_tool_call", _fake_dispatch)
    recorder = start_turn_recording()
    try:
        ctx = _ctx(session_id)
        ctx.messages = [_assistant_calling("web_search")]
        assert await ex.step_tool_execution(ctx, MagicMock(), TraceContext(trace_id="t")) == (
            TaskState.LLM_CALL
        )
        ctx.messages.append(_assistant_calling("fetch_url"))
        assert await ex.step_tool_execution(ctx, MagicMock(), TraceContext(trace_id="t")) == (
            TaskState.LLM_CALL
        )
    finally:
        stop_turn_recording()

    live = _drain(get_event_queue(session_id))
    assert _names(live, "TOOL_CALL_START") == ["web_search", "fetch_url"]
    assert _names(live, "TOOL_CALL_END") == ["web_search", "fetch_url"]
    # Each end follows its own start on the wire.
    kinds = [(e["type"], e["data"]["tool_name"]) for e in live if e["type"].startswith("TOOL_")]
    assert kinds == [
        ("TOOL_CALL_START", "web_search"),
        ("TOOL_CALL_END", "web_search"),
        ("TOOL_CALL_START", "fetch_url"),
        ("TOOL_CALL_END", "fetch_url"),
    ]

    summary = build_turn_summary(recorder.events, now=datetime.now(UTC))
    assert summary is not None
    assert [t.name for t in summary.tools] == _names(live, "TOOL_CALL_START")
    assert [t.status for t in summary.tools] == ["completed", "completed"]


@pytest.mark.asyncio
async def test_parallel_calls_of_one_tool_are_separate_stored_rows_with_their_own_outcome(
    session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FRE-1551: two parallel ``web_search`` calls, the first one finishing last and failing."""

    async def _slow_first(*, tool_call_id: str, tool_name: str, **rest: Any) -> dict[str, Any]:
        result = _ok_result(tool_call_id, tool_name, **rest)
        if tool_call_id == "tc-1":
            await asyncio.sleep(0.05)
            result["success"] = False
        return result

    monkeypatch.setattr(ex, "dispatch_tool_call", _slow_first)
    recorder = start_turn_recording()
    try:
        ctx = _ctx(session_id)
        ctx.messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tc-1", "function": {"name": "web_search", "arguments": "{}"}},
                    {"id": "tc-2", "function": {"name": "web_search", "arguments": "{}"}},
                    {"id": "tc-3", "function": {"name": "fetch_url", "arguments": "{}"}},
                ],
            }
        ]
        await ex.step_tool_execution(ctx, MagicMock(), TraceContext(trace_id="t"))
    finally:
        stop_turn_recording()

    summary = build_turn_summary(recorder.events, now=datetime.now(UTC))
    assert summary is not None
    assert [(t.name, t.status) for t in summary.tools] == [
        ("web_search", "failed"),
        ("web_search", "completed"),
        ("fetch_url", "completed"),
    ]


@pytest.mark.asyncio
async def test_tool_arguments_are_not_copied_onto_the_wire(
    session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel never shows arguments, so ``session_events`` must not store them."""
    monkeypatch.setattr(ex, "dispatch_tool_call", _fake_dispatch)
    ctx = _ctx(session_id)
    ctx.messages = [_assistant_calling("web_search")]

    await ex.step_tool_execution(ctx, MagicMock(), TraceContext(trace_id="t"))

    live = _drain(get_event_queue(session_id))
    starts = [e for e in live if e["type"] == "TOOL_CALL_START"]
    ends = [e for e in live if e["type"] == "TOOL_CALL_END"]
    assert starts[0]["data"]["args"] == {}
    assert ends[0]["data"]["result"] == ""


@pytest.mark.asyncio
async def test_a_failed_or_raising_dispatch_still_closes_its_row(
    session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed tool and a dispatch that raises both end their row, marked ``failed``."""

    async def _mixed(*, tool_call_id: str, tool_name: str, **rest: Any) -> dict[str, Any]:
        if tool_name == "explode":
            raise RuntimeError("boom")
        result = _ok_result(tool_call_id, tool_name, **rest)
        result["success"] = False
        return result

    monkeypatch.setattr(ex, "dispatch_tool_call", _mixed)
    ctx = _ctx(session_id)
    ctx.messages = [_assistant_calling("web_search", "explode")]

    await ex.step_tool_execution(ctx, MagicMock(), TraceContext(trace_id="t"))

    live = _drain(get_event_queue(session_id))
    ends = {
        e["data"]["tool_name"]: e["data"]["result"] for e in live if e["type"] == "TOOL_CALL_END"
    }
    assert ends == {"web_search": "failed", "explode": "failed"}


@pytest.mark.asyncio
async def test_no_session_means_no_tool_events(
    session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn with no session (CLI) has no panel to feed."""
    monkeypatch.setattr(ex, "dispatch_tool_call", _fake_dispatch)
    ctx = _ctx(None)
    ctx.messages = [_assistant_calling("web_search")]

    await ex.step_tool_execution(ctx, MagicMock(), TraceContext(trace_id="t"))

    assert _SeqBuffer.counter == 0
