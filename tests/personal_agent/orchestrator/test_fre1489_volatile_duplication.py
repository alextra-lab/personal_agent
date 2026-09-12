"""FRE-1489 — the tool loop's wire sequence must stay a forward extension.

ADR-0081 D2 requires every prior message to replay byte-identically, so the local
backend can reuse its KV cache as a strict forward extension. The expansion path
appends a second adjacent ``user`` message (the synthesis context), which
``_validate_and_fix_conversation_roles`` merges — and the merge wrote into the caller's
own dict, so every tool-loop iteration appended another copy of the whole
``<turn_context>`` fence to the turn's first message. ADR-0081 D2 point 2 predicted this
interaction with the role fixer.

The measured consequence (FRE-1487): reuse freezes at the first loop call's level for the
rest of the turn, while a turn without expansion advances monotonically.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_QUERY = "I will visit Mallorca Sept 19th. What events are on?"
_SYNTHESIS = (
    "## Sub-agent results\n- worker 1: found the harvest festival\n"
    "Synthesize the results into a coherent response for the user's original question."
)
_FENCE = "<turn_context>"
_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
}


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals() -> Any:
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    saved_layer = _ex._tool_execution_layer
    yield
    _ex._tool_registry = saved_registry
    _ex._tool_execution_layer = saved_layer


def _episode(ident: str, summary: str) -> dict[str, Any]:
    return {
        "type": "episode",
        "conversation_id": ident,
        "user_message": f"earlier question {ident}",
        "summary": summary,
        "key_entities": [],
    }


def _make_ctx(*, hybrid: bool, memory: list[dict[str, Any]] | None = None) -> Any:
    from personal_agent.captains_log.turn_evidence import build_recall_candidates
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext

    messages: list[dict[str, Any]] = [{"role": "user", "content": _QUERY}]
    if hybrid:
        # executor.py:5426 — expansion appends the synthesis context as a second,
        # adjacent user message.
        messages.append({"role": "user", "content": _SYNTHESIS})

    kwargs: dict[str, Any] = {
        "session_id": "test-session",
        "trace_id": "test-trace",
        "user_message": _QUERY,
        "mode": Mode.NORMAL,
        "channel": Channel.CHAT,
        "messages": messages,
    }
    if memory is not None:
        kwargs["memory_context"] = memory
        kwargs["recall_candidates"] = build_recall_candidates(memory, {})
    ctx = ExecutionContext(**kwargs)
    # Fixed clock, never the wall clock, in a test asserting on rendered bytes.
    ctx.turn_started_at = datetime(2026, 9, 10, 15, 18, tzinfo=UTC)
    ctx.salient_highlights = "<salient>the trip is in September</salient>"
    return ctx


async def _drive_loop(
    ctx: Any, rounds: int, monkeypatch: pytest.MonkeyPatch
) -> list[list[dict[str, Any]]]:
    """Drive the real ``step_llm_call`` ``rounds`` times, as the tool loop does.

    Returns:
        One wire-form message list per call, in call order.
    """
    from personal_agent.config import settings
    from personal_agent.orchestrator.executor import build_wire_messages, step_llm_call
    from personal_agent.telemetry.trace import TraceContext

    # Keep the volatile block deterministic: skill routing contributes no bytes here.
    monkeypatch.setattr(settings, "prefer_primitives_enabled", False)

    client = MagicMock()
    client.model_configs = {}
    session = MagicMock()
    session.add_message = AsyncMock()
    session.get_messages = AsyncMock(return_value=[])

    wires: list[list[dict[str, Any]]] = []
    for i in range(rounds):
        client.respond = AsyncMock(
            return_value={
                "content": "",
                "tool_calls": [
                    {"id": f"c{i}", "name": "web_search", "arguments": json.dumps({"query": "x"})}
                ],
                "reasoning_trace": f"reasoning for round {i}",
                "response_id": None,
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )
        with (
            patch("personal_agent.llm_client.factory.get_llm_client", return_value=client),
            patch(
                "personal_agent.orchestrator.executor.get_default_registry",
                return_value=MagicMock(
                    get_tool_definitions_for_llm=MagicMock(return_value=[_TOOL_DEF])
                ),
            ),
        ):
            await step_llm_call(ctx, session, TraceContext.new_trace())

        kwargs = client.respond.call_args.kwargs
        wires.append(build_wire_messages(kwargs["messages"], kwargs.get("system_prompt"), "t"))

        # What step_tool_execution appends (executor.py:7307-7356), reduced to the shape.
        for call in ctx.messages[-1].get("tool_calls") or []:
            ctx.messages.append(
                {
                    "tool_call_id": call["id"],
                    "role": "tool",
                    "name": "web_search",
                    "content": f"tool result {i}",
                }
            )
        ctx.tool_iteration_count += 1
    return wires


def _blob(message: dict[str, Any]) -> str:
    return json.dumps(message, sort_keys=True, ensure_ascii=False)


def _carrier_text(wire: list[dict[str, Any]]) -> str:
    """The first user message — the carrier the fence rides on in a merged request."""
    for message in wire:
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content)
    raise AssertionError("no user message in the wire form")


def _assert_forward_extension(wires: list[list[dict[str, Any]]]) -> None:
    """Every earlier message replays byte-identically; a call only appends (ADR-0081 D2)."""
    for n in range(len(wires) - 1):
        before, after = wires[n], wires[n + 1]
        assert len(after) >= len(before), f"call {n + 1} dropped messages from call {n}"
        for i, (old, new) in enumerate(zip(before, after, strict=False)):
            assert _blob(old) == _blob(new), (
                f"call {n + 1} rewrote message {i} (role {old.get('role')}) — "
                f"the sequence is no longer a forward extension.\n"
                f"was: {_blob(old)[:400]}\nnow: {_blob(new)[:400]}"
            )


class TestHybridToolLoopBytes:
    """The defect: a HYBRID synthesis turn re-merges its synthesis message every call."""

    @pytest.mark.asyncio
    async def test_hybrid_loop_wire_sequence_is_a_forward_extension(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-3's deterministic proxy: no call may rewrite a message an earlier call sent."""
        wires = await _drive_loop(_make_ctx(hybrid=True), 3, monkeypatch)
        _assert_forward_extension(wires)

    @pytest.mark.asyncio
    async def test_carrier_holds_exactly_one_volatile_fence_every_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The duplication, stated as a count.

        Scoped to the carrier: the tool-budget and forced-synthesis injectors append
        their own tail message, which legitimately takes a fence of its own, so the
        invariant is per carrier rather than per request.
        """
        wires = await _drive_loop(_make_ctx(hybrid=True), 3, monkeypatch)
        counts = [_carrier_text(w).count(_FENCE) for w in wires]
        assert counts == [1, 1, 1], f"volatile fence duplicated into the carrier: {counts}"

    @pytest.mark.asyncio
    async def test_request_building_never_rewrites_the_users_own_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ctx.messages[0]`` is the user's turn. Request shaping must not grow it."""
        ctx = _make_ctx(hybrid=True)
        await _drive_loop(ctx, 3, monkeypatch)
        assert ctx.messages[0]["content"] == _QUERY


class TestPlainToolLoopControl:
    """The production control: a turn without expansion advances monotonically."""

    @pytest.mark.asyncio
    async def test_plain_tool_loop_stays_a_forward_extension(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wires = await _drive_loop(_make_ctx(hybrid=False), 3, monkeypatch)
        _assert_forward_extension(wires)

    @pytest.mark.asyncio
    async def test_single_turn_keeps_the_fence_on_the_last_user_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-6: ADR-0081 D1/D2's designed case — the volatile tail rides the query."""
        wires = await _drive_loop(_make_ctx(hybrid=False), 1, monkeypatch)
        carrier = _carrier_text(wires[0])
        assert carrier.lstrip().startswith(_FENCE)
        assert carrier.count(_FENCE) == 1
        assert carrier.rstrip().endswith(_QUERY)


class TestVolatileContentStillReachesTheModel:
    """AC-5: the cheapest way to pass AC-3 is to stop sending the block. Forbid it."""

    @pytest.mark.asyncio
    async def test_memory_section_reaches_the_model_on_every_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        summary = "the owner prefers coastal towns"
        ctx = _make_ctx(hybrid=True, memory=[_episode("t1", summary)])
        wires = await _drive_loop(ctx, 3, monkeypatch)
        for n, wire in enumerate(wires):
            serialized = json.dumps(wire, ensure_ascii=False)
            assert summary in serialized, f"recalled memory missing from call {n}"
            assert ctx.salient_highlights in serialized, f"highlights missing from call {n}"
            assert serialized.count(summary) == 1, (
                f"recalled memory duplicated in call {n}: {serialized.count(summary)} copies"
            )


class TestRoleFixerPurity:
    """The fix's own contract, asserted directly (codex plan-review finding 4).

    The loop-level tests above would pass a fix that copies ``content`` but still writes
    ``tool_calls`` through, so each merge branch is asserted on its own.
    """

    def test_user_string_merge_leaves_the_inputs_untouched(self) -> None:
        from personal_agent.orchestrator.executor import _validate_and_fix_conversation_roles

        first = {"role": "user", "content": "the query"}
        second = {"role": "user", "content": "the synthesis context"}
        out = _validate_and_fix_conversation_roles([first, second])

        assert first["content"] == "the query"
        assert second["content"] == "the synthesis context"
        assert len(out) == 1
        assert "the query" in out[0]["content"] and "the synthesis context" in out[0]["content"]

    def test_assistant_merge_does_not_write_tool_calls_into_the_input(self) -> None:
        from personal_agent.orchestrator.executor import _validate_and_fix_conversation_roles

        first = {"role": "user", "content": "do"}
        prior = {"role": "assistant", "content": "thinking"}
        incoming = {
            "role": "assistant",
            "content": "acting",
            "tool_calls": [{"id": "X1", "type": "function", "function": {"name": "bash"}}],
        }
        out = _validate_and_fix_conversation_roles([first, prior, incoming])

        assert "tool_calls" not in prior, "merge wrote tool_calls into the caller's message"
        assert prior["content"] == "thinking"
        assert out[1]["tool_calls"][0]["id"] == "X1"
        assert "acting" in out[1]["content"]

    def test_block_list_merge_leaves_the_inputs_untouched(self) -> None:
        from personal_agent.orchestrator.executor import _validate_and_fix_conversation_roles

        first = {"role": "user", "content": [{"type": "text", "text": "the query"}]}
        second = {"role": "user", "content": "the synthesis context"}
        out = _validate_and_fix_conversation_roles([first, second])

        assert first["content"] == [{"type": "text", "text": "the query"}]
        assert len(out[0]["content"]) == 2
