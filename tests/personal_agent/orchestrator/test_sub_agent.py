"""Tests for sub-agent runner."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog.testing

from personal_agent.orchestrator.expansion_types import SubAgentMode
from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec


def _spec(task: str = "test task", timeout: float = 30.0) -> SubAgentSpec:
    return SubAgentSpec(
        task=task,
        context=[{"role": "user", "content": "do the thing"}],
        output_format="text",
        max_tokens=1024,
        timeout_seconds=timeout,
    )


def _tooled_spec(tools: list[str], timeout: float = 30.0) -> SubAgentSpec:
    return SubAgentSpec(
        task="discover the request flow",
        context=[{"role": "user", "content": "explore"}],
        output_format="text",
        max_tokens=1024,
        timeout_seconds=timeout,
        tools=tools,
        mode=SubAgentMode.TOOLED_SEQUENTIAL,
    )


def _llm_response(content: str, tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Minimal LLMResponse-shaped dict (real respond returns this; mocks return str)."""
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls or [],
        "usage": {},
        "response_id": None,
        "raw": {},
    }


def _tool_call(call_id: str, name: str, arguments: str = "{}") -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": arguments}


class TestRunSubAgent:
    @pytest.mark.asyncio
    async def test_successful_execution(self) -> None:

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="Sub-agent analysis result")

        result = await run_sub_agent(
            spec=_spec(),
            llm_client=mock_client,
            trace_id="test-trace",
        )
        assert isinstance(result, SubAgentResult)
        assert result.success is True
        assert result.summary == "Sub-agent analysis result"
        assert result.task_id.startswith("sub-")
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_llm_error_returns_failure(self) -> None:

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(side_effect=RuntimeError("LLM overloaded"))

        result = await run_sub_agent(
            spec=_spec(),
            llm_client=mock_client,
            trace_id="test-trace",
        )
        assert result.success is False
        assert "LLM overloaded" in (result.error or "")

    @pytest.mark.asyncio
    async def test_timeout_returns_failure(self) -> None:

        mock_client = AsyncMock()

        async def slow_respond(*args: object, **kwargs: object) -> str:
            await asyncio.sleep(10)
            return "too late"

        mock_client.respond = slow_respond

        result = await run_sub_agent(
            spec=_spec(timeout=0.1),
            llm_client=mock_client,
            trace_id="test-trace",
        )
        assert result.success is False
        assert result.error is not None
        assert "timeout" in result.error.lower() or "Timeout" in result.error

    @pytest.mark.asyncio
    async def test_telemetry_event_emitted(self) -> None:

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="done")

        with structlog.testing.capture_logs() as cap_logs:
            await run_sub_agent(
                spec=_spec(),
                llm_client=mock_client,
                trace_id="t",
            )
        events = [e for e in cap_logs if e.get("event") == "sub_agent_complete"]
        assert len(events) == 1
        assert "task_id" in events[0]
        assert events[0]["success"] is True


class TestTooledLoop:
    """ADR-0086 D3/D4 — the real tool-using discovery loop."""

    def test_shared_dispatch_is_the_same_callable_both_paths(self) -> None:
        """AC#2 — sub-agent and primary executor invoke the SAME dispatch symbol."""
        import personal_agent.orchestrator.executor as ex
        import personal_agent.orchestrator.sub_agent as sa
        from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call

        assert sa.dispatch_tool_call is dispatch_tool_call
        assert ex.dispatch_tool_call is dispatch_tool_call

    @pytest.mark.asyncio
    async def test_tooled_loop_executes_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AC#1 — a TOOLED_SEQUENTIAL sub-agent executes ≥1 tool call and returns content."""
        dispatch = AsyncMock(
            return_value={
                "tool_call_id": "c1",
                "tool_name": "read",
                "content": '{"status":"ok","body":"file contents"}',
                "success": True,
                "latency_ms": 1.0,
            }
        )
        monkeypatch.setattr("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch)

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response("", [_tool_call("c1", "read", '{"path": "/x"}')]),
                _llm_response("FINAL DISCOVERY DIGEST"),
            ]
        )

        result = await run_sub_agent(
            spec=_tooled_spec(tools=["read"]),
            llm_client=mock_client,
            trace_id="t",
            session_id="s",
        )

        assert result.success is True
        assert result.tools_used == ["read"]
        assert result.full_output == "FINAL DISCOVERY DIGEST"
        assert "FINAL DISCOVERY DIGEST" in result.summary
        dispatch.assert_awaited_once()
        assert dispatch.await_args.kwargs["tool_name"] == "read"

    @pytest.mark.asyncio
    async def test_mutating_tool_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AC#3 — a mutating tool (not in the read-only allowlist) is never dispatched."""
        dispatch = AsyncMock()
        monkeypatch.setattr("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch)

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response("", [_tool_call("c1", "write", '{"path": "/x", "content": "y"}')]),
                _llm_response("done without writing"),
            ]
        )

        result = await run_sub_agent(
            spec=_tooled_spec(tools=["read", "write"]),
            llm_client=mock_client,
            trace_id="t",
        )

        assert result.success is True
        assert "write" not in result.tools_used
        dispatch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_iteration_ceiling_forces_final_synthesis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bounded by sub_agent_max_tool_iterations; final pass disables tools."""
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 2)

        dispatch = AsyncMock(
            return_value={
                "tool_call_id": "c",
                "tool_name": "read",
                "content": "{}",
                "success": True,
                "latency_ms": 1.0,
            }
        )
        monkeypatch.setattr("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch)

        mock_client = AsyncMock()
        # Always returns a tool call — never volunteers a final answer.
        mock_client.respond = AsyncMock(
            return_value=_llm_response("", [_tool_call("c", "read", '{"path": "/x"}')])
        )

        result = await run_sub_agent(
            spec=_tooled_spec(tools=["read"]),
            llm_client=mock_client,
            trace_id="t",
        )

        # The forced synthesis here returns empty content → no digest → failure
        # (master review #1: empty discovery must not be a silent success).
        assert result.success is False
        assert result.error is not None
        assert "empty" in result.error.lower()
        # 2 tool rounds + 1 forced synthesis call.
        assert mock_client.respond.await_count == 3
        assert dispatch.await_count == 2
        # The final call offers NO tools (the enforced "can't tool-call" guarantee);
        # we do not pass a dead tool_choice (master review #2).
        final_kwargs = mock_client.respond.await_args.kwargs
        assert final_kwargs.get("tools") is None
        assert "tool_choice" not in final_kwargs

    @pytest.mark.asyncio
    async def test_ceiling_with_nonempty_synthesis_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ceiling reached but the forced synthesis yields content → success."""
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 1)

        dispatch = AsyncMock(
            return_value={
                "tool_call_id": "c",
                "tool_name": "read",
                "content": "{}",
                "success": True,
                "latency_ms": 1.0,
            }
        )
        monkeypatch.setattr("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch)

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response("", [_tool_call("c", "read", '{"path": "/x"}')]),
                _llm_response("SYNTHESIZED DIGEST"),
            ]
        )

        result = await run_sub_agent(
            spec=_tooled_spec(tools=["read"]),
            llm_client=mock_client,
            trace_id="t",
        )

        assert result.success is True
        assert result.full_output == "SYNTHESIZED DIGEST"
        assert result.tools_used == ["read"]

    @pytest.mark.asyncio
    async def test_malformed_tool_call_entry_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-Mapping tool_call entry is skipped; the slice survives (review #3)."""
        dispatch = AsyncMock(
            return_value={
                "tool_call_id": "c1",
                "tool_name": "read",
                "content": "ok",
                "success": True,
                "latency_ms": 1.0,
            }
        )
        monkeypatch.setattr("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch)

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                # One malformed (non-Mapping) entry alongside one valid call.
                _llm_response("", [None, _tool_call("c1", "read", '{"path": "/x"}')]),
                _llm_response("digest despite a malformed call"),
            ]
        )

        result = await run_sub_agent(
            spec=_tooled_spec(tools=["read"]),
            llm_client=mock_client,
            trace_id="t",
        )

        assert result.success is True
        assert result.tools_used == ["read"]
        assert result.full_output == "digest despite a malformed call"
        dispatch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_parallel_inference_unaffected(self) -> None:
        """No-tools PARALLEL_INFERENCE path keeps the str-returning behavior."""
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="plain analysis")

        result = await run_sub_agent(spec=_spec(), llm_client=mock_client, trace_id="t")
        assert result.success is True
        assert result.summary == "plain analysis"
        assert result.tools_used == []
