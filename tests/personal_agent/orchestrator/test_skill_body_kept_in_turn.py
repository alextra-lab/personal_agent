"""ADR-0156 D2.4 (FRE-1577): a skill body loaded in a turn is never pruned within that turn.

Four paths can drop or rewrite a tool result in the middle of a turn. Each keeps a ``read_skill``
result of this turn verbatim:

1. the primary's trim after a context-window rejection (FRE-1527);
2. a worker's trim before its landing call (FRE-1522);
3. the ADR-0085 intra-turn digest;
4. the ADR-0061 hard compression, which summarises the middle of the history.

A ``read_skill`` result from an earlier turn has no such protection: it is history.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.llm_client.history_sanitiser import sanitise_messages
from personal_agent.orchestrator.executor import (
    _trim_messages_for_context_retry,
    _validate_and_fix_conversation_roles,
)
from personal_agent.orchestrator.sub_agent import _trim_for_landing
from personal_agent.orchestrator.tool_result_digest import should_digest
from personal_agent.orchestrator.within_session_compression import compress_in_place

_BODY = "SKILL BODY " * 400


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals() -> Any:
    """step_llm_call caches the patched registry in a module global: restore it."""
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    saved_layer = _ex._tool_execution_layer
    yield
    _ex._tool_registry = saved_registry
    _ex._tool_execution_layer = saved_layer


def _call(call_id: str, name: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _assistant(*calls: dict[str, Any], content: str = "") -> dict[str, Any]:
    return {"role": "assistant", "content": content, "tool_calls": list(calls)}


def _tool(call_id: str, name: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


def _turn() -> list[dict[str, Any]]:
    """An old skill read in history, then this turn: a skill read, two big results."""
    return [
        {"role": "user", "content": "earlier question"},
        _assistant(_call("old", "read_skill")),
        _tool("old", "read_skill", "OLD " + _BODY),
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "Check the logs."},
        _assistant(_call("sk", "read_skill"), _call("b1", "bash")),
        _tool("sk", "read_skill", _BODY),
        _tool("b1", "bash", "x" * 20_000),
        _assistant(_call("b2", "bash")),
        _tool("b2", "bash", "y" * 20_000),
    ]


def _content(messages: list[dict[str, Any]], call_id: str) -> str | None:
    for msg in messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id") == call_id:
            return str(msg["content"])
    return None


def test_the_context_retry_trim_keeps_this_turns_skill_body() -> None:
    trimmed, dropped, _ = _trim_messages_for_context_retry(_turn(), keep_call_ids=frozenset({"sk"}))
    assert _content(trimmed, "sk") == _BODY
    assert _content(
        trimmed,
        "b1",
    ).startswith("[stubbed: ")
    assert _content(trimmed, "old").startswith("[stubbed: ")  # history, not this turn
    assert dropped == 2


def test_a_worker_landing_trim_keeps_its_skill_body() -> None:
    messages = [{"role": "user", "content": "task"}, *_turn()[5:]]
    trimmed, _, _ = _trim_for_landing(messages, context_length=1_000, max_tokens=100)
    assert _content(trimmed, "sk") == _BODY
    assert _content(trimmed, "b1").startswith("[stubbed: ")


def test_the_digest_never_digests_a_skill_body(monkeypatch: pytest.MonkeyPatch) -> None:
    from personal_agent.config import settings

    monkeypatch.setattr(settings, "tool_result_digest_exclude_tools", [])
    assert should_digest("read_skill", _BODY * 10) is False


@pytest.mark.asyncio
async def test_the_hard_compression_keeps_this_turns_skill_exchange() -> None:
    messages = [{"role": "system", "content": "sys"}, *_turn()]
    with (
        patch(
            "personal_agent.orchestrator.within_session_compression.summarize_middle",
            AsyncMock(return_value=("SUMMARY", 1)),
        ) as summarize,
        patch(
            "personal_agent.orchestrator.within_session_compression.record_compression",
            AsyncMock(),
        ),
    ):
        out, _ = await compress_in_place(
            messages,
            trace_id="t",
            session_id="s",
            trigger="hard",
            min_tail_tokens=1,
            max_tail_tokens=1,
            min_tail_turns=1,
            keep_call_ids=frozenset({"sk"}),
        )
    # The summariser never saw this turn's skill body; it did see the old one.
    summarised = str(summarize.call_args.args[0])
    assert _BODY not in summarised.replace("OLD " + _BODY, "")
    assert _content(out, "sk") == _BODY
    assert _content(out, "old") is None
    # head -> kept exchange (only the read_skill call) -> summary -> tail.
    kept_call = next(m for m in out if m.get("tool_calls"))
    assert [c["id"] for c in kept_call["tool_calls"]] == ["sk"]
    summary_at = next(
        i
        for i, m in enumerate(out)
        if m.get("content") == "SUMMARY" or "SUMMARY" in str(m.get("content"))
    )
    assert out.index(kept_call) < summary_at
    # The result survives the role fixer and the wire sanitiser.
    wire, _ = sanitise_messages(_validate_and_fix_conversation_roles(out), emit_telemetry=False)
    assert _content(wire, "sk") == _BODY


@pytest.mark.asyncio
async def test_without_a_summary_the_middle_keeps_its_order() -> None:
    messages = [{"role": "system", "content": "sys"}, *_turn()]
    with (
        patch(
            "personal_agent.orchestrator.within_session_compression.summarize_middle",
            AsyncMock(return_value=("", 1)),
        ),
        patch(
            "personal_agent.orchestrator.within_session_compression.record_compression",
            AsyncMock(),
        ),
    ):
        out, _ = await compress_in_place(
            messages,
            trace_id="t",
            session_id="s",
            trigger="hard",
            min_tail_tokens=1,
            max_tail_tokens=1,
            min_tail_turns=1,
            pre_pass_threshold_tokens=10,
            keep_call_ids=frozenset({"sk"}),
        )
    assert _content(out, "sk") == _BODY
    assert sum(1 for m in out if m.get("tool_call_id") == "sk") == 1
    assert _content(out, "old") != "OLD " + _BODY  # history is still pre-passed


# --- The wiring: the real steps record and pass this turn's skill call ids ------------------


def _ctx_with_call(tool_name: str) -> Any:
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext

    ctx = ExecutionContext(
        session_id="00000000-0000-4000-8000-000000001577",
        trace_id="trace-1577",
        user_message="Check the logs.",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    ctx.messages = [
        {"role": "user", "content": "Check the logs."},
        _assistant({"id": "tc-1", "function": {"name": tool_name, "arguments": "{}"}}),
    ]
    return ctx


def _fake_dispatch(success: bool) -> Any:
    async def dispatch(tool_call_id: str, tool_name: str, *args: Any, **kwargs: Any) -> dict:
        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "content": json.dumps({"ok": success, "body": _BODY}),
            "success": success,
            "latency_ms": 1.0,
            "output_hash": "h" if success else None,
            "gate_result": args[2] if len(args) > 2 else kwargs.get("gate_result"),
            "args_hash": args[1] if len(args) > 1 else kwargs.get("args_hash"),
            "loop_policy": args[3] if len(args) > 3 else kwargs.get("loop_policy"),
            "tool_layer_output": None,
            "tool_layer_error": None,
            "terminal": False,
            "terminal_reason": None,
            "terminal_next_step": None,
        }

    return dispatch


@pytest.mark.parametrize(
    ("tool_name", "success", "recorded"),
    [("read_skill", True, {"tc-1"}), ("read_skill", False, set()), ("bash", True, set())],
)
@pytest.mark.asyncio
async def test_tool_execution_records_a_successful_read_skill_call(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, success: bool, recorded: set[str]
) -> None:
    import personal_agent.orchestrator.executor as ex
    from personal_agent.telemetry.trace import TraceContext

    async def no_status(_ctx: Any) -> None:
        return None

    monkeypatch.setattr(ex, "_get_tool_execution_layer", lambda: object())
    monkeypatch.setattr(ex, "_is_turn_cancelled", lambda _sid: False)
    monkeypatch.setattr(ex, "_report_turn_progress", no_status)
    monkeypatch.setattr(ex, "dispatch_tool_call", _fake_dispatch(success))
    ctx = _ctx_with_call(tool_name)
    await ex.step_tool_execution(ctx, MagicMock(), TraceContext(trace_id="trace-1577"))
    assert ctx.skill_call_ids == recorded


@pytest.mark.asyncio
async def test_the_context_retry_keeps_the_recorded_skill_body() -> None:
    """step_llm_call's context-window retry passes ctx.skill_call_ids to the trim."""
    from litellm.exceptions import ContextWindowExceededError

    from personal_agent.orchestrator.executor import step_llm_call
    from personal_agent.telemetry.trace import TraceContext

    ctx = _ctx_with_call("read_skill")
    ctx.messages = _turn()[4:]
    ctx.skill_call_ids = {"sk"}
    ctx.turn_context_inlined = True
    client = MagicMock()
    client.model_configs = {}
    client.respond = AsyncMock(
        side_effect=[
            ContextWindowExceededError("too long", model="m", llm_provider="openai"),
            {"content": "done", "tool_calls": [], "response_id": None, "usage": {}},
        ]
    )
    session = MagicMock()
    session.add_message = AsyncMock()
    session.get_messages = AsyncMock(return_value=[])
    with (
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=client),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        ),
    ):
        await step_llm_call(ctx, session, TraceContext.new_trace())

    assert client.respond.call_count == 2
    retried = client.respond.call_args_list[1].kwargs["messages"]
    assert _content(retried, "sk") == _BODY
    assert str(_content(retried, "b1")).startswith("[stubbed: ")


# --- Dedup in dispatch_tool_call: only a body read is deduplicated ---------------------------


def _layer(output: dict[str, Any]) -> Any:
    from types import SimpleNamespace

    from personal_agent.tools.read_skill import read_skill_tool

    layer = MagicMock()
    layer.registry.get_tool = MagicMock(return_value=(read_skill_tool, None))
    layer.execute_tool = AsyncMock(
        return_value=SimpleNamespace(
            success=True, output=output, error=None, error_detail=None, latency_ms=1, metadata={}
        )
    )
    return layer


async def _dispatch(layer: Any, arguments: dict[str, str], loaded: set[str]) -> dict[str, Any]:
    from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call
    from personal_agent.telemetry.trace import TraceContext

    return await dispatch_tool_call(
        tool_call_id="c1",
        tool_name="read_skill",
        arguments=arguments,
        tool_layer=layer,
        trace_ctx=TraceContext(trace_id="t"),
        trace_id="t",
        session_id=None,
        loaded_skills=loaded,
    )


@pytest.mark.asyncio
async def test_a_second_body_read_returns_the_marker() -> None:
    loaded: set[str] = set()
    layer = _layer({"skill_name": "query-tempo", "body": _BODY, "files": []})
    first = await _dispatch(layer, {"name": "query-tempo"}, loaded)
    second = await _dispatch(layer, {"name": "query-tempo"}, loaded)
    assert _BODY in first["content"]
    assert "already loaded earlier in this turn" in second["content"]
    assert layer.execute_tool.await_count == 1
    assert loaded == {"query-tempo"}


@pytest.mark.asyncio
async def test_a_file_read_is_never_deduplicated_and_loads_no_name() -> None:
    loaded: set[str] = {"query-tempo"}
    layer = _layer({"skill_name": "query-tempo", "path": "references/a.md", "content": "FILE"})
    result = await _dispatch(layer, {"name": "query-tempo", "path": "references/a.md"}, loaded)
    assert "FILE" in result["content"]
    fresh: set[str] = set()
    await _dispatch(layer, {"name": "query-tempo", "path": "references/a.md"}, fresh)
    assert fresh == set()


@pytest.mark.asyncio
async def test_the_order_is_head_kept_summary_then_a_user_anchored_tail() -> None:
    tail = [
        {"role": "user", "content": "You have 2 tool calls left."},
        {"role": "assistant", "content": "Understood."},
    ]
    messages = [{"role": "system", "content": "sys"}, *_turn(), *tail]
    with (
        patch(
            "personal_agent.orchestrator.within_session_compression.summarize_middle",
            AsyncMock(return_value=("SUMMARY", 1)),
        ),
        patch(
            "personal_agent.orchestrator.within_session_compression.record_compression",
            AsyncMock(),
        ),
    ):
        out, _ = await compress_in_place(
            messages,
            trace_id="t",
            session_id="s",
            trigger="hard",
            min_tail_tokens=1,
            max_tail_tokens=10_000,
            min_tail_turns=2,
            keep_call_ids=frozenset({"sk"}),
        )
    roles = [(m["role"], m.get("tool_call_id") or m.get("content", "")[:20]) for m in out]
    assert roles == [
        ("system", "sys"),
        ("user", "earlier question"),
        ("assistant", ""),
        ("tool", "sk"),
        ("assistant", "SUMMARY"),
        ("user", "You have 2 tool call"),
        ("assistant", "Understood."),
    ]
