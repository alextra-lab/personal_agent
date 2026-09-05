"""Tests for sub-agent runner."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
import structlog.testing

from personal_agent.llm_client.types import GenerationProgress
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


def _spec_with_tools(
    tools: list[str], timeout: float = 30.0, hard_deadline: float | None = None
) -> SubAgentSpec:
    return SubAgentSpec(
        task="test task",
        context=[{"role": "user", "content": "do the thing"}],
        output_format="text",
        max_tokens=1024,
        timeout_seconds=timeout,
        hard_deadline_seconds=hard_deadline,
        tools=tools,
    )


def _spec_with_denied_tools(denied_tools: tuple[str, ...], timeout: float = 30.0) -> SubAgentSpec:
    return SubAgentSpec(
        task="test task",
        context=[{"role": "user", "content": "do the thing"}],
        output_format="text",
        max_tokens=1024,
        timeout_seconds=timeout,
        denied_tools=denied_tools,
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
    async def test_denied_tools_threads_from_spec_into_result_on_success(self) -> None:
        """FRE-1388: a refusal recorded on the spec survives into the result

        the primary reads — not just a log line at the point of refusal.
        """
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="Sub-agent analysis result")

        result = await run_sub_agent(
            spec=_spec_with_denied_tools(("bash",)),
            llm_client=mock_client,
            trace_id="test-trace",
        )
        assert result.success is True
        assert result.denied_tools == ("bash",)

    @pytest.mark.asyncio
    async def test_denied_tools_threads_into_result_on_timeout(self) -> None:
        """FRE-1388: the refusal survives a killed sub-agent too."""
        mock_client = AsyncMock()

        async def slow_respond(*args: object, **kwargs: object) -> str:
            await asyncio.sleep(10)
            return "too late"

        mock_client.respond = slow_respond

        result = await run_sub_agent(
            spec=_spec_with_denied_tools(("bash",), timeout=0.1),
            llm_client=mock_client,
            trace_id="test-trace",
        )
        assert result.success is False
        assert result.denied_tools == ("bash",)

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


def _stub_tool_layer(*tool_names: str) -> MagicMock:
    """A get_shared_tool_execution_layer() stub advertising ``tool_names``."""
    layer = MagicMock()
    layer.registry.get_tool_definitions_for_llm.return_value = [
        {"type": "function", "function": {"name": n, "description": "d", "parameters": {}}}
        for n in tool_names
    ]
    return layer


def _dispatch_result(tool_call_id: str, tool_name: str, content: str) -> dict[str, Any]:
    return {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "content": content,
        "success": True,
        "latency_ms": 1.0,
        "output_hash": "h",
        "gate_result": None,
        "args_hash": "",
        "loop_policy": None,
        "tool_layer_output": None,
        "tool_layer_error": None,
    }


class TestNormalizeToolCalls:
    """FRE-1389 — per-round unique tool_call ids, mirroring executor's own scheme."""

    def test_ids_differ_across_rounds_for_the_same_raw_id(self) -> None:
        from personal_agent.orchestrator.sub_agent import _normalize_tool_calls

        raw = [{"id": "call_0", "name": "run_python", "arguments": "{}"}]
        round1 = _normalize_tool_calls(raw, 1)
        round2 = _normalize_tool_calls(raw, 2)
        assert round1[0]["id"] != round2[0]["id"]

    def test_shape_matches_openai_format(self) -> None:
        from personal_agent.orchestrator.sub_agent import _normalize_tool_calls

        raw = [{"id": "call_0", "name": "run_python", "arguments": '{"a": 1}'}]
        normalized = _normalize_tool_calls(raw, 1)
        assert normalized[0]["type"] == "function"
        assert normalized[0]["function"] == {"name": "run_python", "arguments": '{"a": 1}'}
        assert normalized[0]["index"] == 0


class TestExtractStatedToolGap:
    def test_strips_trailing_sentinel_line(self) -> None:
        from personal_agent.orchestrator.sub_agent import _extract_stated_tool_gap

        content, gap = _extract_stated_tool_gap("Here is my answer.\nTOOL_GAP: web_search")
        assert gap == "web_search"
        assert "TOOL_GAP" not in content
        assert content == "Here is my answer."

    def test_no_sentinel_is_a_noop(self) -> None:
        from personal_agent.orchestrator.sub_agent import _extract_stated_tool_gap

        content, gap = _extract_stated_tool_gap("Just a normal answer.")
        assert gap is None
        assert content == "Just a normal answer."

    def test_sentinel_must_be_the_last_line(self) -> None:
        from personal_agent.orchestrator.sub_agent import _extract_stated_tool_gap

        content, gap = _extract_stated_tool_gap("TOOL_GAP: web_search\nmore text after")
        assert gap is None
        assert "TOOL_GAP" in content


class TestBuildToolDefs:
    def test_empty_grant_returns_none(self) -> None:
        from personal_agent.orchestrator.sub_agent import _build_tool_defs

        assert _build_tool_defs([]) is None

    def test_filters_to_granted_subset(self) -> None:
        from personal_agent.orchestrator.sub_agent import _build_tool_defs

        with patch(
            "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
            return_value=_stub_tool_layer("run_python", "bash", "read"),
        ):
            defs = _build_tool_defs(["run_python"])
        assert defs is not None
        assert [d["function"]["name"] for d in defs] == ["run_python"]


class TestEffectiveHardDeadline:
    """A tool-using loop's deadline must scale with its own iteration cap —

    the single-call sizing (worker_hard_deadline_seconds: "60s generation +
    25s queue-wait absorption") predates this loop and would otherwise kill a
    genuine multi-round tool-using sub-agent well before it ever reaches its
    own cap.
    """

    def test_no_tools_keeps_single_call_sizing(self) -> None:
        from personal_agent.orchestrator.sub_agent import _effective_hard_deadline

        assert _effective_hard_deadline(_spec(timeout=60.0)) == 60.0

    def test_tools_scale_by_iteration_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.config import settings
        from personal_agent.orchestrator.sub_agent import _effective_hard_deadline

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 5)

        assert _effective_hard_deadline(_spec_with_tools(["run_python"], timeout=60.0)) == 300.0

    def test_explicit_hard_deadline_still_wins_if_larger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.config import settings
        from personal_agent.orchestrator.sub_agent import _effective_hard_deadline

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 5)
        spec = _spec_with_tools(["run_python"], timeout=60.0, hard_deadline=1000.0)

        assert _effective_hard_deadline(spec) == 1000.0


class TestSubAgentToolLoop:
    """FRE-1389 — the sub-agent's own bounded tool loop."""

    @pytest.mark.asyncio
    async def test_tool_round_trip_populates_tools_used(self) -> None:
        """AC-1: a granted tool is actually called, and it shows up as used."""
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response(
                    "",
                    tool_calls=[{"id": "c0", "name": "run_python", "arguments": '{"code": "1"}'}],
                ),
                _llm_response("final answer"),
            ]
        )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c0", "run_python", "42")),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=mock_client, trace_id="t"
            )

        assert result.success is True
        assert result.tools_used == ["run_python"]
        assert result.tool_iterations == 1
        assert result.summary == "final answer"

    @pytest.mark.asyncio
    async def test_empty_grant_never_passes_tools_to_respond(self) -> None:
        """Regression: a grant-less sub-agent must behave exactly as before the loop."""
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="plain answer")

        await run_sub_agent(spec=_spec(), llm_client=mock_client, trace_id="t")

        _, kwargs = mock_client.respond.call_args
        assert kwargs["tools"] is None

    @pytest.mark.asyncio
    async def test_iteration_cap_stops_the_loop_with_explicit_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-2: the cap is the sub-agent's own number, and hitting it is a
        distinct failure — not a disguised empty success.
        """
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 2)

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            return_value=_llm_response(
                "still working",
                tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}],
            )
        )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c0", "run_python", "ok")),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=mock_client, trace_id="t"
            )

        assert result.success is False
        assert "tool iteration limit" in (result.error or "")
        assert result.tool_iterations == 2
        # Two executed rounds, plus the third call whose batch was refused.
        assert mock_client.respond.call_count == 3

    @pytest.mark.asyncio
    async def test_tool_outside_grant_is_refused_without_dispatch(self) -> None:
        """AC-3: seeded negative — an out-of-grant attempt is refused, never dispatched."""
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response(
                    "",
                    tool_calls=[
                        {"id": "c0", "name": "run_python", "arguments": "{}"},
                        {"id": "c1", "name": "bash", "arguments": "{}"},
                    ],
                ),
                _llm_response("final answer"),
            ]
        )
        dispatch_mock = AsyncMock(return_value=_dispatch_result("c0", "run_python", "ok"))

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch_mock),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=mock_client, trace_id="t"
            )

        assert result.refused_tool_attempts == ("bash",)
        assert result.tools_used == ["run_python"]
        assert dispatch_mock.call_count == 1
        assert dispatch_mock.call_args.kwargs["tool_name"] == "run_python"

    @pytest.mark.asyncio
    async def test_malformed_arguments_are_refused_without_dispatch(self) -> None:
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response(
                    "",
                    tool_calls=[{"id": "c0", "name": "run_python", "arguments": "not json"}],
                ),
                _llm_response("final answer"),
            ]
        )
        dispatch_mock = AsyncMock()

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch_mock),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=mock_client, trace_id="t"
            )

        assert result.success is True
        assert result.tools_used == []
        assert result.refused_tool_attempts == ()
        dispatch_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_tool_result_chars_absorbed_measures_isolation(self) -> None:
        """AC-4: raw tool-result chars are counted even though they never reach summary."""
        raw_tool_output = "x" * 500
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response(
                    "", tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}]
                ),
                _llm_response("ok"),
            ]
        )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c0", "run_python", raw_tool_output)),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=mock_client, trace_id="t"
            )

        assert result.tool_result_chars_absorbed >= 500
        assert len(result.summary) < result.tool_result_chars_absorbed

    @pytest.mark.asyncio
    async def test_multi_round_cost_is_summed(self) -> None:
        """AC-6: every round's cost_usd is summed, not just the last call's."""
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response_with_cost(
                    "", 0.01, tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}]
                ),
                _llm_response_with_cost("done", 0.02),
            ]
        )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c0", "run_python", "ok")),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=mock_client, trace_id="t"
            )

        assert result.cost_usd == pytest.approx(0.03)

    @pytest.mark.asyncio
    async def test_stated_tool_gap_is_parsed_and_stripped_from_summary(self) -> None:
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="Did what I could.\nTOOL_GAP: web_search")

        result = await run_sub_agent(spec=_spec(), llm_client=mock_client, trace_id="t")

        assert result.stated_tool_gap == "web_search"
        assert "TOOL_GAP" not in result.summary

    @pytest.mark.asyncio
    async def test_whole_loop_deadline_not_per_call(self) -> None:
        """The hard deadline bounds the ENTIRE loop, not each respond() call alone."""

        async def _always_wants_more_tools(*args: object, **kwargs: object) -> dict[str, Any]:
            await asyncio.sleep(0.1)
            return _llm_response(
                "", tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}]
            )

        mock_client = AsyncMock()
        mock_client.respond = _always_wants_more_tools

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c0", "run_python", "ok")),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"], timeout=0.05, hard_deadline=0.15),
                llm_client=mock_client,
                trace_id="t",
            )

        # Each individual respond() call (0.1s) is well under the 0.15s hard
        # deadline; only the SUM across rounds exceeds it.
        assert result.success is False
        assert "Timeout" in (result.error or "")

    @pytest.mark.asyncio
    async def test_completed_round_cost_survives_a_later_timeout(self) -> None:
        """A paid round's cost is not lost when a LATER round is what times out."""

        async def _first_cheap_then_hangs(*args: object, **kwargs: object) -> dict[str, Any] | str:
            if not hasattr(_first_cheap_then_hangs, "called"):
                _first_cheap_then_hangs.called = True  # type: ignore[attr-defined]
                return _llm_response_with_cost(
                    "", 0.05, tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}]
                )
            await asyncio.sleep(10)
            return "too late"

        mock_client = AsyncMock()
        mock_client.respond = _first_cheap_then_hangs

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c0", "run_python", "ok")),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"], timeout=0.05, hard_deadline=0.15),
                llm_client=mock_client,
                trace_id="t",
            )

        assert result.success is False
        assert result.cost_usd == pytest.approx(0.05)
        assert result.tools_used == ["run_python"]


class TestPartialProgressOnKill:
    """FRE-1379 AC-1 — a killed sub-agent reports what it managed.

    A stub client that streams slowly (advancing a caller-supplied
    ``progress_sink`` between awaits, exactly like the real streaming client
    would) and never returns before the hard deadline. Before this ticket the
    result carried an empty ``full_output``/``summary`` and no token or
    elapsed-generation figures — the "digest_chars=0, full_output_chars=0"
    black hole the ticket exists to close.
    """

    @staticmethod
    async def _slow_streaming_respond(*args: object, **kwargs: object) -> str:
        progress: GenerationProgress | None = kwargs.get("progress_sink")  # type: ignore[assignment]
        if progress is not None:
            progress.generation_started_monotonic = time.monotonic()
        for word in ("partial", "words", "so", "far"):
            if progress is not None:
                progress.content += word + " "
            await asyncio.sleep(0.05)
        return "too late"

    @pytest.mark.asyncio
    async def test_outer_hard_deadline_reports_partial_content_and_tokens(self) -> None:
        mock_client = AsyncMock()
        mock_client.respond = self._slow_streaming_respond

        result = await run_sub_agent(
            spec=_spec(timeout=0.05, hard_deadline=0.15),
            llm_client=mock_client,
            trace_id="test-trace",
        )

        assert result.success is False
        assert "Timeout" in (result.error or "")
        # The stub had appended at least one word by 0.15s (each step is 0.05s).
        assert result.full_output.strip() != ""
        assert result.summary == result.full_output
        assert result.tokens_generated == len(result.full_output.split())
        assert result.tokens_generated > 0
        assert result.elapsed_generation_ms is not None
        assert result.elapsed_generation_ms >= 0

    @pytest.mark.asyncio
    async def test_capture_written_on_kill_carries_partial_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import personal_agent.orchestrator.sub_agent as sa

        captured: list[Any] = []
        monkeypatch.setattr(sa, "write_sub_agent_capture", lambda cap: captured.append(cap))

        mock_client = AsyncMock()
        mock_client.respond = self._slow_streaming_respond

        result = await run_sub_agent(
            spec=_spec(timeout=0.05, hard_deadline=0.15),
            llm_client=mock_client,
            trace_id="t",
        )

        assert len(captured) == 1
        cap = captured[0]
        assert cap.success is False
        assert cap.full_output == result.full_output
        assert cap.full_output_chars > 0
        assert cap.tokens_generated == result.tokens_generated
        assert cap.elapsed_generation_ms == result.elapsed_generation_ms


class TestGenerationMetricsOnSuccess:
    """FRE-1379 — tokens_generated/elapsed_generation_ms exist uniformly.

    Populated on success too (not just on a killed sub-agent) so a fan-out's
    survivors and its casualties are comparable on the same fields.
    """

    @pytest.mark.asyncio
    async def test_populated_when_client_uses_progress_sink(self) -> None:
        async def streaming_respond(*args: object, **kwargs: object) -> str:
            progress: GenerationProgress | None = kwargs.get("progress_sink")  # type: ignore[assignment]
            if progress is not None:
                progress.generation_started_monotonic = time.monotonic()
                progress.content = "the final answer"
            return "the final answer"

        mock_client = AsyncMock()
        mock_client.respond = streaming_respond

        result = await run_sub_agent(spec=_spec(), llm_client=mock_client, trace_id="t")

        assert result.success is True
        assert result.tokens_generated == len("the final answer".split())
        assert result.elapsed_generation_ms is not None
        assert result.elapsed_generation_ms >= 0

    @pytest.mark.asyncio
    async def test_absent_when_client_ignores_progress_sink(self) -> None:
        """Back-compat: a mock/cloud client that never touches progress_sink."""
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="plain reply here")

        result = await run_sub_agent(spec=_spec(), llm_client=mock_client, trace_id="t")

        assert result.success is True
        assert result.tokens_generated == result.token_count
        assert result.elapsed_generation_ms is None


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
    async def test_capture_carries_tool_loop_activity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FRE-1389: tool-loop fields reach the audit record, not just the result."""
        import personal_agent.orchestrator.sub_agent as sa

        captured: list[Any] = []
        monkeypatch.setattr(sa, "write_sub_agent_capture", lambda cap: captured.append(cap))

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response(
                    "",
                    tool_calls=[
                        {"id": "c0", "name": "run_python", "arguments": "{}"},
                        {"id": "c1", "name": "bash", "arguments": "{}"},
                    ],
                ),
                _llm_response("Done.\nTOOL_GAP: web_search"),
            ]
        )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c0", "run_python", "x" * 50)),
            ),
        ):
            await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=mock_client, trace_id="t"
            )

        assert len(captured) == 1
        cap = captured[0]
        assert cap.tool_iterations == 1
        assert cap.tool_result_chars_absorbed >= 50
        assert cap.refused_tool_attempts == ["bash"]
        assert cap.stated_tool_gap == "web_search"

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


class TestDigestCapAndClipVisibility:
    """FRE-1387 — the digest cap no longer fires in normal operation, and when it
    does, the clip is visible as its own WARNING event, not just a computed ratio.
    """

    @pytest.mark.asyncio
    async def test_real_sized_output_is_not_clipped(self) -> None:
        """AC-1 — output well within the old 2000-char cap but past it (the
        measured p90 of real sub-agent output, ~11,425 chars) now fits whole.
        """
        content = "x" * 11_425
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value=content)

        result = await run_sub_agent(spec=_spec(), llm_client=mock_client, trace_id="t")

        assert result.summary == content
        assert len(result.summary) == len(result.full_output)

    @pytest.mark.asyncio
    async def test_output_under_new_cap_emits_no_clip_warning(self) -> None:
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="x" * 100)

        with structlog.testing.capture_logs() as cap_logs:
            await run_sub_agent(spec=_spec(), llm_client=mock_client, trace_id="t")

        assert not [e for e in cap_logs if e.get("event") == "sub_agent_output_clipped"]

    @pytest.mark.asyncio
    async def test_output_over_new_cap_is_clipped_and_warned(self) -> None:
        """AC-2 — a clip fires its own WARNING event, distinct from the INFO
        completion event that already carried truncation_ratio unread.
        """
        content = "x" * 30_000
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value=content)

        with structlog.testing.capture_logs() as cap_logs:
            result = await run_sub_agent(
                spec=_spec(), llm_client=mock_client, trace_id="t", session_id="s"
            )

        assert len(result.summary) == 25_000
        assert len(result.full_output) == 30_000

        warnings = [e for e in cap_logs if e.get("event") == "sub_agent_output_clipped"]
        assert len(warnings) == 1
        w = warnings[0]
        assert w["log_level"] == "warning"
        assert w["trace_id"] == "t"
        assert w["session_id"] == "s"
        assert w["full_output_chars"] == 30_000
        assert w["digest_chars"] == 25_000
        assert w["discarded_chars"] == 5_000
        assert w["truncation_ratio"] == pytest.approx(25_000 / 30_000)

    @pytest.mark.asyncio
    async def test_clip_warning_is_allowlisted_for_error_pattern_scan(self) -> None:
        """The event name must be in error_monitor's WARNING_EVENT_ALLOWLIST or
        the ADR-0056 scan never picks it up regardless of how often it fires.
        """
        from personal_agent.telemetry.error_monitor import WARNING_EVENT_ALLOWLIST

        assert "sub_agent_output_clipped" in WARNING_EVENT_ALLOWLIST

    @pytest.mark.asyncio
    async def test_killed_worker_over_cap_is_clipped_and_warned(self) -> None:
        """The killed-result path shares the same cap and must warn identically."""

        async def _slow_over_cap(*args: object, **kwargs: object) -> str:
            progress: GenerationProgress | None = kwargs.get("progress_sink")  # type: ignore[assignment]
            if progress is not None:
                progress.generation_started_monotonic = time.monotonic()
                progress.content = "y" * 26_000
            await asyncio.sleep(10)
            return "too late"

        mock_client = AsyncMock()
        mock_client.respond = _slow_over_cap

        with structlog.testing.capture_logs() as cap_logs:
            result = await run_sub_agent(
                spec=_spec(timeout=0.05), llm_client=mock_client, trace_id="t"
            )

        assert result.success is False
        assert len(result.summary) == 25_000
        warnings = [e for e in cap_logs if e.get("event") == "sub_agent_output_clipped"]
        assert len(warnings) == 1


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
