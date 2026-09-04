"""Tests for sub-agent runner."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import structlog.testing

from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec


def _spec(
    task: str = "test task", timeout: float = 30.0, hard_deadline: float | None = None
) -> SubAgentSpec:
    return SubAgentSpec(
        task=task,
        context=[{"role": "user", "content": "do the thing"}],
        output_format="text",
        max_tokens=1024,
        timeout_seconds=timeout,
        hard_deadline_seconds=hard_deadline,
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
        # FRE-517: task_id is a real UUID (keys the (trace_id, task_id) route-trace segment row).
        assert isinstance(result.task_id, UUID)
        assert result.duration_ms >= 0
        assert result.tools_used == []

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
    async def test_generation_timeout_passed_to_client(self) -> None:
        """FRE-1374 — timeout_s reaches llm_client.respond so a real client can bound
        generation from concurrency-slot acquisition, not from spawn.
        """
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="ok")

        await run_sub_agent(spec=_spec(timeout=42.0), llm_client=mock_client, trace_id="t")

        _, kwargs = mock_client.respond.call_args
        assert kwargs["timeout_s"] == 42.0

    @pytest.mark.asyncio
    async def test_queue_wait_does_not_shrink_generation_budget(self) -> None:
        """AC-1 — a call that runs longer than the OLD single nominal budget still
        completes, because the outer bound is now the separate, larger hard deadline.
        """
        mock_client = AsyncMock()

        async def slow_but_within_hard_deadline(*args: object, **kwargs: object) -> str:
            await asyncio.sleep(0.15)
            return "done"

        mock_client.respond = slow_but_within_hard_deadline

        result = await run_sub_agent(
            spec=_spec(timeout=0.05, hard_deadline=0.3),
            llm_client=mock_client,
            trace_id="test-trace",
        )
        assert result.success is True
        assert result.summary == "done"

    @pytest.mark.asyncio
    async def test_outer_hard_deadline_reports_actual_duration_not_nominal_budget(self) -> None:
        """AC-2 — the shortfall is visible: the message reflects real elapsed time,
        not the nominal generation budget that used to be hard-coded into it.
        """
        mock_client = AsyncMock()

        async def hangs(*args: object, **kwargs: object) -> str:
            await asyncio.sleep(10)
            return "too late"

        mock_client.respond = hangs

        result = await run_sub_agent(
            spec=_spec(timeout=0.05, hard_deadline=0.2),
            llm_client=mock_client,
            trace_id="test-trace",
        )
        assert result.success is False
        assert result.error is not None
        assert "0.05" not in result.error
        assert result.duration_ms == pytest.approx(200, abs=100)

    @pytest.mark.asyncio
    async def test_hard_deadline_clamped_above_generation_timeout(self) -> None:
        """A hard_deadline_seconds smaller than timeout_seconds (e.g. a bad override)
        must not resurrect the old bug by cutting generation short of its own budget.
        """
        mock_client = AsyncMock()

        async def slow(*args: object, **kwargs: object) -> str:
            await asyncio.sleep(0.15)
            return "done"

        mock_client.respond = slow

        # hard_deadline (0.01) is deliberately smaller than timeout_seconds (0.2) —
        # the clamp must use timeout_seconds as the floor.
        result = await run_sub_agent(
            spec=_spec(timeout=0.2, hard_deadline=0.01),
            llm_client=mock_client,
            trace_id="test-trace",
        )
        assert result.success is True

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

    @pytest.mark.asyncio
    async def test_start_and_complete_carry_session_id(self) -> None:
        """ADR-0086 D7 / ADR-0074: discovery events join under the session anchor.

        ``walk.py:_walk_es_agent_logs`` finds events by ``term session_id``; without
        ``session_id`` the start/complete events are invisible to the joinability
        walk. The complete event also carries ``digest_chars`` (the digest size that
        crosses into the parent synthesis context).
        """
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="done")

        with structlog.testing.capture_logs() as cap_logs:
            await run_sub_agent(
                spec=_spec(),
                llm_client=mock_client,
                trace_id="t",
                session_id="sess-1",
            )

        start = [e for e in cap_logs if e.get("event") == "sub_agent_start"]
        complete = [e for e in cap_logs if e.get("event") == "sub_agent_complete"]
        assert len(start) == 1
        assert start[0]["session_id"] == "sess-1"
        assert len(complete) == 1
        assert complete[0]["session_id"] == "sess-1"
        assert isinstance(complete[0]["digest_chars"], int)


class TestInputContextSummary:
    """FRE-505: structured breakdown of what a sub-agent was fed."""

    def test_detects_memory_marker(self) -> None:
        from personal_agent.orchestrator.sub_agent import _summarize_input_context

        spec = SubAgentSpec(
            task="t",
            context=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "## Your Memory Graph — Known Entities\n- x"},
            ],
        )
        summary = _summarize_input_context("system prompt body", spec)

        assert summary["memory_in_context"] is True
        assert summary["context_message_count"] == 2
        assert summary["system_prompt_chars"] == len("system prompt body")
        assert summary["context_chars"] == len("hello") + len(
            "## Your Memory Graph — Known Entities\n- x"
        )
        assert summary["context_messages"][0] == {
            "role": "user",
            "chars": 5,
            "content_preview": "hello",
        }

    def test_no_memory_marker(self) -> None:
        from personal_agent.orchestrator.sub_agent import _summarize_input_context

        spec = SubAgentSpec(task="t", context=[{"role": "user", "content": "plain"}])
        summary = _summarize_input_context("sys", spec)

        assert summary["memory_in_context"] is False

    def test_handles_missing_keys(self) -> None:
        from personal_agent.orchestrator.sub_agent import _summarize_input_context

        spec = SubAgentSpec(task="t", context=[{"role": "user"}, {"content": "c"}])
        summary = _summarize_input_context("sys", spec)

        assert summary["context_message_count"] == 2
        assert summary["context_messages"][0]["chars"] == 0


class TestSubAgentCaptureEmitted:
    """FRE-505: a per-sub-agent audit record is written on every terminal path."""

    @pytest.mark.asyncio
    async def test_capture_written_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import personal_agent.orchestrator.sub_agent as sa

        captured: list[Any] = []
        monkeypatch.setattr(sa, "write_sub_agent_capture", lambda cap: captured.append(cap))

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="x" * 5000)

        result = await run_sub_agent(
            spec=_spec(), llm_client=mock_client, trace_id="t", session_id="s"
        )

        assert len(captured) == 1
        cap = captured[0]
        assert cap.trace_id == "t"
        assert cap.session_id == "s"
        # FRE-517: the capture keys on the stringified UUID (ES/wire boundary stays str).
        assert cap.task_id == str(result.task_id)
        assert cap.injected_digest == result.summary
        assert cap.full_output == result.full_output
        assert cap.full_output_chars == 5000
        assert 0.0 < cap.truncation_ratio <= 1.0
        assert cap.success is True

    @pytest.mark.asyncio
    async def test_capture_written_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import personal_agent.orchestrator.sub_agent as sa

        captured: list[Any] = []
        monkeypatch.setattr(sa, "write_sub_agent_capture", lambda cap: captured.append(cap))

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(side_effect=RuntimeError("boom"))

        await run_sub_agent(spec=_spec(), llm_client=mock_client, trace_id="t")

        assert len(captured) == 1
        cap = captured[0]
        assert cap.success is False
        assert cap.truncation_ratio == 0.0
        assert cap.full_output == ""

    @pytest.mark.asyncio
    async def test_capture_written_on_cancellation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Global dispatch timeout cancels the coroutine — the audit record still fires."""
        import personal_agent.orchestrator.sub_agent as sa

        captured: list[Any] = []
        monkeypatch.setattr(sa, "write_sub_agent_capture", lambda cap: captured.append(cap))

        mock_client = AsyncMock()

        async def _cancelled(*args: object, **kwargs: object) -> str:
            raise asyncio.CancelledError()

        mock_client.respond = _cancelled

        with pytest.raises(asyncio.CancelledError):
            await run_sub_agent(spec=_spec(), llm_client=mock_client, trace_id="t")

        assert len(captured) == 1
        assert captured[0].success is False
        assert "cancel" in (captured[0].error or "").lower()


def _llm_response_with_cost(
    content: str, cost: float, tool_calls: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """An LLMResponse-shaped dict carrying a per-call cost_usd (paid/cloud calls)."""
    resp = _llm_response(content, tool_calls)
    resp["cost_usd"] = cost
    return resp


class TestSubAgentCost:
    """FRE-501 — per-call cost_usd is captured and summed onto SubAgentResult."""

    @pytest.mark.asyncio
    async def test_default_path_captures_cost_from_mapping(self) -> None:
        """The PARALLEL_INFERENCE path keeps the mapping's cost_usd and content."""
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value=_llm_response_with_cost("analysis", 0.0123))

        result = await run_sub_agent(spec=_spec(), llm_client=mock_client, trace_id="t")

        assert result.success is True
        # Content is parsed from the mapping (not str(dict)) — fixes a latent bug.
        assert result.summary == "analysis"
        assert result.cost_usd == pytest.approx(0.0123)

    @pytest.mark.asyncio
    async def test_default_path_bare_string_is_zero_cost(self) -> None:
        """A bare-string response (free/local or test mock) yields cost_usd 0.0."""
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="plain string")

        result = await run_sub_agent(spec=_spec(), llm_client=mock_client, trace_id="t")

        assert result.cost_usd == 0.0
        assert result.summary == "plain string"

    @pytest.mark.asyncio
    async def test_cost_surfaced_on_complete_telemetry(self) -> None:
        """sub_agent_complete carries cost_usd for the post-deploy cross-check."""
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value=_llm_response_with_cost("done", 0.005))

        with structlog.testing.capture_logs() as cap_logs:
            await run_sub_agent(spec=_spec(), llm_client=mock_client, trace_id="t")

        complete = [e for e in cap_logs if e.get("event") == "sub_agent_complete"]
        assert complete[0]["cost_usd"] == pytest.approx(0.005)
