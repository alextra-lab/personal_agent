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

from typing import Any
from unittest.mock import AsyncMock, patch

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
