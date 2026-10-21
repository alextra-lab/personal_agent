"""FRE-1639 — the helper landing call keeps its rounds' prompt-cache prefix.

AC-1: the landing request's prefix is byte-identical to the last round's request.
AC-2: the cause field is ``response_format`` (``text`` on the Responses wire).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import personal_agent.orchestrator.sub_agent as sa
from personal_agent.llm_client.models import Dialect
from personal_agent.llm_client.responses_route import to_responses_request
from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import SubAgentSpec
from personal_agent.orchestrator.worker_types import WorkerType

_TOOL_CALL = {"id": "c0", "name": "web_search", "arguments": '{"query": "zorptal7"}'}
_REPORT = json.dumps(
    {
        "working_notes": "notes",
        "findings": [
            {
                "claim": "Found ZORPTAL7",
                "source_url": "https://a.example/one",
                "date_or_period": "",
                "why_it_matters": "relevant",
            }
        ],
        "gaps": [],
        "tool_gap": "",
    }
)


def _spec() -> SubAgentSpec:
    return SubAgentSpec(
        task="find ZORPTAL7",
        context=[],
        max_tokens=1024,
        timeout_seconds=30.0,
        tools=["web_search"],
        worker_type=WorkerType.RESEARCHER,
        thoroughness="quick",
    )


def _response(
    content: str, tool_calls: list[dict[str, Any]] | None = None, finish_reason: str = "stop"
) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls or [],
        "usage": {},
        "response_id": None,
        "raw": {},
        "finish_reason": finish_reason,
    }


def _tool_layer() -> MagicMock:
    layer = MagicMock()
    layer.registry.get_tool_definitions_for_llm.return_value = [
        {
            "type": "function",
            "function": {"name": "web_search", "description": "d", "parameters": {}},
        }
    ]
    return layer


def _client(
    replies: list[Callable[[], dict[str, Any]]], dialect: Dialect
) -> tuple[AsyncMock, list[dict[str, Any]]]:
    """A client that scripts its replies and snapshots every request at call time.

    ``state.messages`` is one list the loop keeps appending to, so a snapshot taken
    after the run would show the landing's messages for every call.
    """
    snapshots: list[dict[str, Any]] = []

    async def _respond(**kwargs: Any) -> dict[str, Any]:
        snapshots.append(
            {
                key: json.loads(json.dumps(kwargs.get(key)))
                for key in ("messages", "tools", "tool_choice", "response_format")
            }
        )
        return replies[len(snapshots) - 1]()

    client = AsyncMock()
    client.respond = AsyncMock(side_effect=_respond)
    client.dialect_for_role = MagicMock(return_value=dialect)
    client.provider = "openai"
    return client, snapshots


async def _run(client: AsyncMock) -> Any:
    with (
        patch.object(sa, "get_shared_tool_execution_layer", return_value=_tool_layer()),
        patch.object(
            sa, "dispatch_tool_call", AsyncMock(return_value={"content": "a tool result"})
        ),
    ):
        return await run_sub_agent(spec=_spec(), llm_client=client, trace_id="t")


def _voluntary_stop(dialect: Dialect) -> tuple[AsyncMock, list[dict[str, Any]]]:
    return _client(
        [
            lambda: _response("", [_TOOL_CALL]),
            lambda: _response("DONE"),
            lambda: _response(_REPORT),
        ],
        dialect,
    )


def _forced_synthesis(dialect: Dialect) -> tuple[AsyncMock, list[dict[str, Any]]]:
    """A round whose tool call is cut at the token ceiling lands through ``_forced_synthesis``."""
    return _client(
        [
            lambda: _response("", [_TOOL_CALL]),
            lambda: _response("", [_TOOL_CALL], finish_reason="length"),
            lambda: _response(_REPORT),
        ],
        dialect,
    )


def _assert_prefix_kept(snapshots: list[dict[str, Any]]) -> None:
    last_round, landing = snapshots[-2], snapshots[-1]
    assert json.dumps(landing["tools"]) == json.dumps(last_round["tools"])
    count = len(last_round["messages"])
    assert json.dumps(landing["messages"][:count]) == json.dumps(last_round["messages"])
    assert landing["messages"][-1]["role"] == "user"
    assert "worker_report_v1" in landing["messages"][-1]["content"]
    assert landing["response_format"] is None
    assert landing["tool_choice"] == "none"


class TestAC1PrefixIsByteIdentical:
    @pytest.mark.asyncio
    async def test_voluntary_stop_landing_keeps_the_round_prefix(self) -> None:
        client, snapshots = _voluntary_stop(Dialect.OPENAI_GPT5)
        result = await _run(client)

        assert len(snapshots) == 3
        _assert_prefix_kept(snapshots)
        assert result.report_kind == "synthesized"
        assert result.report is not None

    @pytest.mark.asyncio
    async def test_forced_synthesis_landing_keeps_the_round_prefix(self) -> None:
        client, snapshots = _forced_synthesis(Dialect.OPENAI_GPT5)
        result = await _run(client)

        assert len(snapshots) == 3
        _assert_prefix_kept(snapshots)
        assert "Do NOT call any more tools" in snapshots[-1]["messages"][-2]["content"]
        assert result.report_kind == "synthesized"

    @pytest.mark.asyncio
    async def test_the_schema_message_reaches_the_trim_and_state_is_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The suffix counts toward the fit, and the trim's no-op return is not ``state.messages``."""
        seen: list[list[dict[str, Any]]] = []
        states: list[Any] = []
        real_trim = sa._trim_for_landing
        real_landing = sa._write_landing_report

        def _spy_trim(messages: list[dict[str, Any]], *args: Any) -> Any:
            seen.append(messages)
            return real_trim(messages, *args)

        async def _spy_landing(state: Any, *args: Any, **kwargs: Any) -> Any:
            states.append(state)
            return await real_landing(state, *args, **kwargs)

        monkeypatch.setattr(sa, "_trim_for_landing", _spy_trim)
        monkeypatch.setattr(sa, "_write_landing_report", _spy_landing)
        # No context length -> the trim returns its input list unchanged (a no-op case).
        monkeypatch.setattr(sa, "_resolve_deployment_context_length", lambda client: None)
        client, snapshots = _voluntary_stop(Dialect.OPENAI_GPT5)
        await _run(client)

        (trim_input,) = seen
        (state,) = states
        assert "worker_report_v1" in trim_input[-1]["content"]
        assert trim_input is not state.messages
        assert all("worker_report_v1" not in str(m["content"]) for m in state.messages)
        assert len(snapshots[-1]["messages"]) == len(state.messages) + 1

    @pytest.mark.asyncio
    async def test_a_prose_reply_is_still_a_ledger(self) -> None:
        client, snapshots = _client(
            [
                lambda: _response("", [_TOOL_CALL]),
                lambda: _response("DONE"),
                lambda: _response("A prose report, not JSON."),
            ],
            Dialect.OPENAI_GPT5,
        )
        result = await _run(client)

        assert snapshots[-1]["response_format"] is None
        assert result.report_kind == "ledger"
        assert result.report is None

    @pytest.mark.asyncio
    async def test_a_grammar_dialect_keeps_response_format_and_adds_no_message(self) -> None:
        client, snapshots = _voluntary_stop(Dialect.LLAMACPP_QWEN)
        await _run(client)

        last_round, landing = snapshots[-2], snapshots[-1]
        assert landing["response_format"] is not None
        extra = landing["messages"][len(last_round["messages"]) :]
        assert all("worker_report_v1" not in str(m["content"]) for m in extra)


def _wire(snapshot: dict[str, Any]) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": "openai/gpt-6.1-sol",
        "messages": snapshot["messages"],
        "tools": snapshot["tools"],
        "max_tokens": 1024,
    }
    if snapshot["tool_choice"] is not None:
        request["tool_choice"] = snapshot["tool_choice"]
    if snapshot["response_format"] is not None:
        request["response_format"] = snapshot["response_format"]
    return to_responses_request(request, client=MagicMock())


class TestAC2TheCauseIsResponseFormat:
    """On the Responses wire the schema is the ``text`` key, and OpenAI renders it before the history."""

    @pytest.mark.asyncio
    async def test_the_old_landing_carried_text_and_the_new_one_does_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Seeded negative: with the fix switched off, the landing wire request carries `text`.
        monkeypatch.setattr(sa, "landing_schema_breaks_prefix_cache", lambda dialect: False)
        client, snapshots = _voluntary_stop(Dialect.OPENAI_GPT5)
        await _run(client)
        old_round, old_landing = _wire(snapshots[-2]), _wire(snapshots[-1])
        assert "text" not in old_round
        assert old_landing["text"]["format"]["type"] == "json_schema"

        monkeypatch.undo()
        client, snapshots = _voluntary_stop(Dialect.OPENAI_GPT5)
        await _run(client)
        new_round, new_landing = _wire(snapshots[-2]), _wire(snapshots[-1])
        assert "text" not in new_landing
        assert new_landing["tools"] == new_round["tools"]
        assert new_landing.get("instructions") == new_round.get("instructions")
        assert new_landing["input"][: len(new_round["input"])] == new_round["input"]
