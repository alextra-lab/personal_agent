"""FRE-1529 — the volatile block rides one message per turn, and is chosen once.

Four sites append a user-role message mid-turn: the tool-budget warning, the
forced-synthesis prompt, the cite-only grounding retry and the worker-synthesis prompt.
Before this fix, each one became the new "last user message", so the next
``step_llm_call`` round inlined a second, third and fourth ``<turn_context>`` fence, and
with it the whole ``<skill_library>`` block (trace 91b57b5c: 154,096 prompt tokens).

Since ADR-0156 D2 (FRE-1577) no skill body rides the fence, so these tests mark the fence with
the session highlights, another member of the same volatile block.

These tests drive the real ``step_llm_call`` across tool rounds, as the loop does, and
read what reached the model.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_QUERY = "Check the logs and tell me what failed overnight."
_FENCE = "<turn_context>"
_LIBRARY = "SALIENT HIGHLIGHTS MARKER"
_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a command.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
    },
}
_PRIOR_TURN = [
    {
        "role": "user",
        "content": "<turn_context>\nOLD VOLATILE\n</turn_context>\n\nthe first question",
    },
    {"role": "assistant", "content": "the first answer"},
]


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals() -> Any:
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    saved_layer = _ex._tool_execution_layer
    yield
    _ex._tool_registry = saved_registry
    _ex._tool_execution_layer = saved_layer


def _make_ctx(history: list[dict[str, Any]] | None = None) -> Any:
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext

    messages = [dict(m) for m in history or []] + [{"role": "user", "content": _QUERY}]
    ctx = ExecutionContext(
        session_id="00000000-0000-4000-8000-000000001529",
        trace_id="test-trace",
        user_message=_QUERY,
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=messages,
    )
    # Fixed clock, never the wall clock, in a test asserting on rendered bytes.
    ctx.turn_started_at = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    ctx.salient_highlights = _LIBRARY
    return ctx


def _budget_warning(ctx: Any) -> None:
    """Put the loop two calls from its limit: the call itself injects the warning."""
    from personal_agent.orchestrator.executor import _resolve_max_iterations

    ctx.tool_iteration_count = max(ctx.tool_iteration_count, _resolve_max_iterations(ctx) - 2)


def _forced_synthesis(ctx: Any) -> None:
    """The iteration limit fired: the call itself injects the synthesis prompt."""
    ctx.force_synthesis_from_limit = True


def _cite_only_retry(ctx: Any) -> None:
    """ADR-0151 D3: step_synthesis appends the retry directive, then re-enters LLM_CALL."""
    ctx.messages.append({"role": "user", "content": "Cite only. Do not call tools."})
    ctx.grounding_retry_pending = True


def _appended_user_message(ctx: Any) -> None:
    """Any other mid-turn user-role message (the worker-synthesis prompt's shape)."""
    ctx.messages.append({"role": "user", "content": "Synthesize the results."})


async def _drive_loop(
    ctx: Any,
    rounds: int,
    monkeypatch: pytest.MonkeyPatch,
    before_round: Callable[[int, Any], None] | None = None,
) -> list[dict[str, Any]]:
    """Drive the real ``step_llm_call`` ``rounds`` times, as the tool loop does.

    Returns:
        The ``respond()`` kwargs of each call, in call order.
    """
    from personal_agent.orchestrator.executor import step_llm_call
    from personal_agent.telemetry.trace import TraceContext

    client = MagicMock()
    client.model_configs = {}
    client.dialect_for_role = MagicMock(return_value=None)  # forced synthesis reads it
    session = MagicMock()
    session.add_message = AsyncMock()
    session.get_messages = AsyncMock(return_value=[])

    sent: list[dict[str, Any]] = []
    for i in range(rounds):
        if before_round is not None:
            before_round(i, ctx)
        client.respond = AsyncMock(
            return_value={
                "content": "",
                "tool_calls": [
                    {"id": f"c{i}", "name": "bash", "arguments": json.dumps({"command": "ls"})}
                ],
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

        sent.append(dict(client.respond.call_args.kwargs))
        # What step_tool_execution appends, reduced to the shape.
        for call in ctx.messages[-1].get("tool_calls") or []:
            ctx.messages.append(
                {"tool_call_id": call["id"], "role": "tool", "name": "bash", "content": "ok"}
            )
        ctx.tool_iteration_count += 1
    return sent


def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else json.dumps(content)


def _count(messages: list[dict[str, Any]], needle: str) -> int:
    return sum(_text(m).count(needle) for m in messages)


_INJECTORS = {
    "budget_warning": _budget_warning,
    "forced_synthesis": _forced_synthesis,
    "cite_only_retry": _cite_only_retry,
    "appended_user_message": _appended_user_message,
}


class TestOneBlockPerTurn:
    """AC-1: a mid-turn user message never receives a copy of the block."""

    @pytest.mark.asyncio
    async def test_round_two_after_budget_warning_has_one_skill_library(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The incident's shape: warnings on rounds 2 and 3, one block on every call."""

        def warn_from_round_two(i: int, ctx: Any) -> None:
            if i >= 1:
                _budget_warning(ctx)

        ctx = _make_ctx()
        sent = await _drive_loop(ctx, 3, monkeypatch, warn_from_round_two)

        warnings = [m for m in sent[-1]["messages"] if "tool budget" in _text(m).lower()]
        assert warnings, "the budget warning never fired — the test proves nothing"
        assert [_count(s["messages"], _LIBRARY) for s in sent] == [1, 1, 1]
        assert [_count(s["messages"], _FENCE) for s in sent] == [1, 1, 1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("injector", sorted(_INJECTORS))
    async def test_every_mid_turn_injector_leaves_one_block(
        self, injector: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each of the four injection shapes, on its own."""

        def inject_on_round_two(i: int, ctx: Any) -> None:
            if i == 1:
                _INJECTORS[injector](ctx)

        sent = await _drive_loop(_make_ctx(), 2, monkeypatch, inject_on_round_two)

        user_messages = [m for m in sent[-1]["messages"] if m.get("role") == "user"]
        assert len(user_messages) >= 2 or injector == "cite_only_retry", (
            f"{injector} added no user message — the test proves nothing"
        )
        assert _count(sent[-1]["messages"], _LIBRARY) == 1

    @pytest.mark.asyncio
    async def test_the_block_rides_the_users_own_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The query owns the fence. The injected warning stays as written."""

        def warn(i: int, ctx: Any) -> None:
            if i == 1:
                _budget_warning(ctx)

        ctx = _make_ctx()
        await _drive_loop(ctx, 2, monkeypatch, warn)

        fenced = [i for i, m in enumerate(ctx.messages) if _text(m).lstrip().startswith(_FENCE)]
        assert fenced == [0]
        assert ctx.messages[0]["content"].rstrip().endswith(_QUERY)


class TestPrefixStaysStable:
    """AC-2: the frozen prefix replays byte-identically, so KV reuse is unaffected."""

    @pytest.mark.asyncio
    async def test_prior_turns_and_system_prompt_byte_identical_across_rounds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def warn(i: int, ctx: Any) -> None:
            if i >= 1:
                _budget_warning(ctx)

        ctx = _make_ctx(history=_PRIOR_TURN)
        sent = await _drive_loop(ctx, 3, monkeypatch, warn)

        systems = {s["system_prompt"] for s in sent}
        assert len(systems) == 1, "message[0] (the system prompt) changed between rounds"
        for s in sent:
            assert s["messages"][: len(_PRIOR_TURN)] == _PRIOR_TURN
        queries = {_text(s["messages"][len(_PRIOR_TURN)]) for s in sent[1:]}
        # Round 1 may carry the /no_think suffix on its query; rounds 2+ must agree.
        assert len(queries) == 1, "the query message changed between rounds"
        assert ctx.messages[: len(_PRIOR_TURN)] == _PRIOR_TURN

    @pytest.mark.asyncio
    async def test_next_turn_gets_its_own_single_fence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flag is per request: turn N+1 inlines, and leaves turn N's bytes alone."""

        def warn(i: int, ctx: Any) -> None:
            if i >= 1:
                _budget_warning(ctx)

        first = _make_ctx()
        await _drive_loop(first, 2, monkeypatch, warn)
        persisted = [dict(m) for m in first.messages] + [
            {"role": "assistant", "content": "the answer"}
        ]

        second = _make_ctx(history=persisted)
        sent = await _drive_loop(second, 2, monkeypatch, warn)

        for s in sent:
            assert s["messages"][: len(persisted)] == persisted
            assert _count(s["messages"][len(persisted) :], _LIBRARY) == 1
