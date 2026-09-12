"""Tests for sub-agent runner."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
import structlog.testing

from personal_agent.llm_client.types import GenerationProgress
from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec
from personal_agent.orchestrator.worker_types import WORKER_TYPES, Thoroughness, WorkerType


def _spec(
    task: str = "test task", timeout: float = 30.0, hard_deadline: float | None = None
) -> SubAgentSpec:
    return SubAgentSpec(
        task=task,
        context=[{"role": "user", "content": "do the thing"}],
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
        max_tokens=1024,
        timeout_seconds=timeout,
        hard_deadline_seconds=hard_deadline,
        tools=tools,
    )


def _spec_with_denied_tools(denied_tools: tuple[str, ...], timeout: float = 30.0) -> SubAgentSpec:
    return SubAgentSpec(
        task="test task",
        context=[{"role": "user", "content": "do the thing"}],
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
    async def test_denied_tools_threads_into_result_on_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FRE-1388: the refusal survives a killed sub-agent too."""
        from personal_agent.config import settings

        # FRE-1444: the outer deadline is the budget plus this allowance, so a test
        # whose mock client ignores its generation budget must shrink the allowance to
        # keep the net firing inside a test's patience.
        monkeypatch.setattr(settings, "worker_queue_absorption_seconds", 0.05)
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
    async def test_timeout_returns_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.config import settings

        # FRE-1444: see test_denied_tools_threads_into_result_on_timeout.
        monkeypatch.setattr(settings, "worker_queue_absorption_seconds", 0.05)
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

    the single-call sizing (the generation budget plus
    ``worker_queue_absorption_seconds``) predates this loop and would otherwise kill a
    genuine multi-round tool-using sub-agent well before it ever reaches its
    own cap.
    """

    def test_no_tools_keeps_single_call_sizing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FRE-1444: the single-call sizing is derived now, so it exceeds the budget.

        It used to equal it (60.0) — the collapsed state ADR-0145 D1 removes, since a
        deadline equal to the generation timeout can never fire.
        """
        from personal_agent.config import settings
        from personal_agent.orchestrator.sub_agent import _effective_hard_deadline

        monkeypatch.setattr(settings, "worker_queue_absorption_seconds", 25.0)

        assert _effective_hard_deadline(_spec(timeout=60.0), 60.0) == 85.0

    def test_tools_scale_by_iteration_cap_plus_the_synthesis_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0149: N rounds AND the forced-synthesis call that follows them.

        Was ``N`` budgets. A capped worker's last inference used to be one of the
        N rounds; since ADR-0149 it is a separate tools-off call that writes the
        report, so a net sized at N would cut exactly the call the ADR adds —
        and the landing reserve, which wants ``mean_round + one budget`` of
        headroom, would fire before the first round instead of near the end.
        """
        from personal_agent.config import settings
        from personal_agent.orchestrator.sub_agent import _effective_hard_deadline

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 5)

        assert (
            _effective_hard_deadline(_spec_with_tools(["run_python"], timeout=60.0), 60.0) == 360.0
        )

    def test_explicit_hard_deadline_still_wins_if_larger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.config import settings
        from personal_agent.orchestrator.sub_agent import _effective_hard_deadline

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 5)
        spec = _spec_with_tools(["run_python"], timeout=60.0, hard_deadline=1000.0)

        assert _effective_hard_deadline(spec, 60.0) == 1000.0


class TestMaxDeadlineSecondsOverride:
    """FRE-1397 — a dispatcher can shrink a sub-agent's own deadline to fit

    whatever remains of the turn's budget, without touching
    ``_effective_hard_deadline`` itself (every existing caller/test of that
    function is unaffected).
    """

    @pytest.mark.asyncio
    async def test_smaller_cap_wins_over_effective_deadline(self) -> None:
        mock_client = AsyncMock()

        async def hangs(*args: object, **kwargs: object) -> str:
            await asyncio.sleep(10)
            return "too late"

        mock_client.respond = hangs

        result = await run_sub_agent(
            spec=_spec(timeout=0.05, hard_deadline=5.0),
            llm_client=mock_client,
            trace_id="test-trace",
            max_deadline_seconds=0.2,
        )
        assert result.success is False
        assert result.duration_ms == pytest.approx(200, abs=100)

    @pytest.mark.asyncio
    async def test_cap_never_extends_the_effective_deadline(self) -> None:
        """A cap larger than the spec's own deadline is a no-op — min(), never max()."""
        mock_client = AsyncMock()

        async def hangs(*args: object, **kwargs: object) -> str:
            await asyncio.sleep(10)
            return "too late"

        mock_client.respond = hangs

        result = await run_sub_agent(
            spec=_spec(timeout=0.05, hard_deadline=0.2),
            llm_client=mock_client,
            trace_id="test-trace",
            max_deadline_seconds=5.0,
        )
        assert result.success is False
        assert result.duration_ms == pytest.approx(200, abs=100)

    @pytest.mark.asyncio
    async def test_none_preserves_todays_behavior(self) -> None:
        """Default (no cap) leaves the spec's own deadline exactly as computed today."""
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="ok")

        result = await run_sub_agent(
            spec=_spec(timeout=0.05, hard_deadline=0.3),
            llm_client=mock_client,
            trace_id="test-trace",
            max_deadline_seconds=None,
        )
        assert result.success is True


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
    async def test_dispatch_is_called_with_the_sub_agent_principal(self) -> None:
        """FRE-1473 — every tool call this loop dispatches identifies as the sub-agent
        principal, so ``dispatch_tool_call`` can apply its parameter ceiling (AC-1).
        """
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response(
                    "",
                    tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}],
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
            await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=mock_client, trace_id="t"
            )

        assert dispatch_mock.call_args.kwargs["principal"] == "sub_agent"

    @pytest.mark.asyncio
    async def test_the_refusal_names_the_tool_to_the_sub_agent(self) -> None:
        """FRE-1463 AC-3 — the refusal reaches the sub-agent and names the tool.

        The AC's failure clause is "the request silently returns nothing — the
        sub-agent then reports an absence it cannot distinguish from an empty
        result". So the audience is the sub-agent, and the proof is the tool
        message it reads on the next round, not a log line.
        """
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response(
                    "", tool_calls=[{"id": "c0", "name": "fetch_url", "arguments": "{}"}]
                ),
                _llm_response("final answer"),
            ]
        )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch("personal_agent.orchestrator.sub_agent.dispatch_tool_call", AsyncMock()),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=mock_client, trace_id="t"
            )

        assert result.refused_tool_attempts == ("fetch_url",)
        second_round_messages = mock_client.respond.call_args_list[1].kwargs["messages"]
        tool_messages = [m for m in second_round_messages if m.get("role") == "tool"]
        assert tool_messages, "the sub-agent saw no tool message at all"
        assert "fetch_url" in tool_messages[-1]["content"]
        assert "not available" in tool_messages[-1]["content"]

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
        """The hard deadline bounds the ENTIRE loop, not each respond() call alone.

        FRE-1496 raised the default ``sub_agent_max_tool_iterations`` cap 5 -> 20.
        That scales ``_effective_hard_deadline``'s tool-granted sizing
        (``effective_timeout * (cap + 1)``) for this spec's 0.05s timeout from
        0.3s to 1.05s — enough room for the ADR-0149 D3 landing reserve to stop
        the loop gracefully with a ledger BEFORE the outer ``asyncio.wait_for``
        would kill it, so the terminal state moved from a raw ``Timeout`` to a
        ``time_reserve`` stop. That is the reserve doing exactly what it exists
        to do, not a regression (checked against main: this test's assertion is
        the only thing that changed, not the loop's own logic).

        So the invariant this test guards is that the loop never outlives its
        own hard deadline — not which of its two designed exits fires. It is
        measured directly (wall clock), not inferred from an error string.
        """
        from personal_agent.orchestrator.sub_agent import _effective_hard_deadline

        async def _always_wants_more_tools(*args: object, **kwargs: object) -> dict[str, Any]:
            await asyncio.sleep(0.1)
            return _llm_response(
                "", tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}]
            )

        mock_client = AsyncMock()
        mock_client.respond = _always_wants_more_tools

        spec = _spec_with_tools(["run_python"], timeout=0.05, hard_deadline=0.15)
        hard_deadline = _effective_hard_deadline(spec, 0.05)

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
            started = time.monotonic()
            result = await run_sub_agent(spec=spec, llm_client=mock_client, trace_id="t")
            elapsed = time.monotonic() - started

        # The hard bound: generous CI slack, but this fails if the deadline is
        # not respected.
        assert elapsed <= hard_deadline + 1.0, (
            f"loop ran {elapsed:.2f}s against a {hard_deadline:.2f}s hard deadline"
        )
        assert result.success is False

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


class TestIterationCapNarrative:
    """FRE-1399, as ADR-0149 leaves it — a capped worker always reports something.

    FRE-1399's own mechanism was to join every round's assistant text, because
    the cap path used to keep only the CAPPING round's ``response_content`` and
    that is frequently empty. ADR-0149 replaces the mechanism and keeps the
    obligation: the cap now forces one tools-off call, so the report is written
    rather than salvaged. The round texts survive as the ledger's "Model notes
    per round" on the paths where no synthesis text exists, and
    ``narrative_synthesized`` keeps its original meaning — the worker did work
    and no model-written report describes it.
    """

    @pytest.mark.asyncio
    async def test_cap_forces_a_synthesis_call_that_writes_the_report(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0149 D3 move 5: the call after the last round writes, it does not search.

        Was FRE-1399's ``test_recovers_earlier_round_text_even_when_capping_round_is_empty``,
        which asserted the terminal content was the joined round texts. That is
        exactly what the ADR removed: on 2026-09-10 those joined texts were five
        stage directions against 154,755 absorbed characters.
        """
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 2)

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response(
                    "Now let me check the first source",
                    tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}],
                ),
                _llm_response(
                    "", tool_calls=[{"id": "c1", "name": "run_python", "arguments": "{}"}]
                ),
                # The cap fires BEFORE this call, so it is dispatched tools-off
                # and the model writes instead of emitting a third tool batch.
                _llm_response("Finding: the answer is 42, from source S."),
            ]
        )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c", "run_python", "ok")),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=mock_client, trace_id="t"
            )

        assert result.stop_reason == "cap"
        assert result.report_kind == "synthesized"
        assert result.summary == "Finding: the answer is 42, from source S."
        # The narration is NOT the report. That is the whole change.
        assert "Now let me check" not in result.summary
        assert result.narrative_synthesized is False
        # Stopping at the cap is not finishing the task (FRE-1389 AC-2).
        assert result.success is False
        # Two executed rounds plus the synthesis call — the same call count as
        # before, with the last one writing rather than being discarded.
        assert mock_client.respond.call_count == 3
        assert result.tool_iterations == 2

    @pytest.mark.asyncio
    async def test_round_texts_survive_in_the_ledger_when_synthesis_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every round's text is kept, in order, and reaches the ledger.

        Was ``test_multiple_rounds_of_text_are_joined_in_order``. The accumulation
        it guards is still real and still load-bearing — it is now the ledger's
        "Model notes per round" section rather than the report itself, so the
        seeded distinction it existed for ("recovered one earlier round" versus
        "concatenates several") is preserved on the path where it still applies.
        """
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 2)

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response(
                    "Alpha finding",
                    tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}],
                ),
                _llm_response(
                    "Beta finding",
                    tool_calls=[{"id": "c1", "name": "run_python", "arguments": "{}"}],
                ),
                # The synthesis call itself returns nothing.
                _llm_response(""),
            ]
        )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c", "run_python", "ok")),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=mock_client, trace_id="t"
            )

        assert result.stop_reason == "cap"
        assert result.report_kind == "ledger"
        assert result.narrative_synthesized is True
        assert "Alpha finding" in result.summary
        assert "Beta finding" in result.summary
        assert result.summary.index("Alpha finding") < result.summary.index("Beta finding")

    @pytest.mark.asyncio
    async def test_no_assistant_text_anywhere_gets_synthesized_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-1: a worker that never emitted text still reports something, never ''."""
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 1)

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            return_value=_llm_response(
                "", tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}]
            )
        )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c", "run_python", "x" * 42)),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=mock_client, trace_id="t"
            )

        assert result.success is False
        assert result.summary != ""
        assert result.narrative_synthesized is True
        assert str(result.tool_result_chars_absorbed) in result.summary

    @pytest.mark.asyncio
    async def test_whitespace_only_round_text_is_not_treated_as_narrative(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex plan-review: '' and ' \\n' must both count as "no narrative"."""
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 1)

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            return_value=_llm_response(
                " \n", tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}]
            )
        )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c", "run_python", "ok")),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=mock_client, trace_id="t"
            )

        assert result.narrative_synthesized is True
        assert result.summary.strip() != ""

    @pytest.mark.asyncio
    async def test_synthesized_narrative_emits_its_own_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-3: visible without reading source — mirrors sub_agent_output_clipped."""
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 1)

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            return_value=_llm_response(
                "", tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}]
            )
        )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c", "run_python", "ok")),
            ),
            structlog.testing.capture_logs() as cap_logs,
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"]),
                llm_client=mock_client,
                trace_id="t",
                session_id="s",
            )

        warnings = [
            e for e in cap_logs if e.get("event") == "sub_agent_iteration_cap_narrative_synthesized"
        ]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["trace_id"] == "t"
        assert warnings[0]["session_id"] == "s"
        assert warnings[0]["tool_iterations"] == result.tool_iterations
        assert warnings[0]["tool_result_chars_absorbed"] == result.tool_result_chars_absorbed

    @pytest.mark.asyncio
    async def test_real_narrative_emits_no_synthesized_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 1)

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
                AsyncMock(return_value=_dispatch_result("c", "run_python", "ok")),
            ),
            structlog.testing.capture_logs() as cap_logs,
        ):
            await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=mock_client, trace_id="t"
            )

        assert not [
            e for e in cap_logs if e.get("event") == "sub_agent_iteration_cap_narrative_synthesized"
        ]

    @pytest.mark.asyncio
    async def test_synthesized_warning_is_allowlisted(self) -> None:
        from personal_agent.telemetry.error_monitor import WARNING_EVENT_ALLOWLIST

        assert "sub_agent_iteration_cap_narrative_synthesized" in WARNING_EVENT_ALLOWLIST

    @pytest.mark.asyncio
    async def test_capture_carries_narrative_synthesized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-4: the field reaches the audit record, not just the in-process result."""
        import personal_agent.orchestrator.sub_agent as sa
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 1)
        captured: list[Any] = []
        monkeypatch.setattr(sa, "write_sub_agent_capture", lambda cap: captured.append(cap))

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            return_value=_llm_response(
                "", tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}]
            )
        )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c", "run_python", "ok")),
            ),
        ):
            await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=mock_client, trace_id="t"
            )

        assert len(captured) == 1
        assert captured[0].narrative_synthesized is True


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
        # ADR-0149: the content is now the ledger, which QUOTES the partial
        # rather than being it. `tokens_generated` still counts what the model
        # generated — counting the ledger's own deterministic prose would
        # inflate every killed worker's figure with text this process wrote.
        assert result.stop_reason == "deadline"
        assert result.report_kind == "ledger"
        assert "Partial text from the interrupted call:" in result.full_output
        assert result.tokens_generated > 0
        assert result.tokens_generated < len(result.full_output.split())
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
        # ADR-0149: an upstream error is a declared terminal path with a ledger,
        # not the empty record it wrote before. `full_output == ""` was the
        # defect — a worker that raised looked identical to one that found
        # nothing, on the one surface anyone reads afterwards.
        assert cap.stop_reason == "error"
        assert cap.report_kind == "ledger"
        assert "boom" in cap.full_output
        assert cap.truncation_ratio == 1.0

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
    async def test_killed_worker_over_cap_is_clipped_and_warned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The killed-result path shares the same cap and must warn identically."""
        from personal_agent.config import settings

        # FRE-1444: the outer deadline is the budget plus this allowance.
        monkeypatch.setattr(settings, "worker_queue_absorption_seconds", 0.05)

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


def _llm_response_with_finish_reason(
    content: str, finish_reason: str | None, tool_calls: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """An LLMResponse-shaped dict carrying an explicit finish_reason (ADR-0150 D6)."""
    resp = _llm_response(content, tool_calls)
    resp["finish_reason"] = finish_reason
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


# ==========================================================================
# ADR-0149 — land before the cut (FRE-1482)
# ==========================================================================


_MARKERS = ("ZORPTAL", "QUVIREX", "MELDWAY")
_SOURCES = ("brevik.example", "santolan.example", "kirrowe.example")


def _seeded_tool_content(idx: int) -> str:
    """A stub tool result carrying a coined marker and its coined source.

    Coined so the test OWNS the token: a marker cannot be in the model's
    parametric recall, so its absence from the report is a real failure rather
    than an ambiguity. This is the distinction ADR-0147 drew when it rejected a
    live lexical check and accepted a seeded one.
    """
    return f"Result: event {_MARKERS[idx]} takes place, per {_SOURCES[idx]}."


class TestForcedSynthesisReportsEvidence:
    """ADR-0149 AC-1 — a capped worker's report carries its findings, not its narration."""

    @staticmethod
    def _client(rounds: int) -> AsyncMock:
        """A stub model that narrates while it has tools and reports when it does not."""
        client = AsyncMock()
        calls: list[dict[str, Any]] = []

        async def _respond(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            if kwargs.get("tool_choice") == "none":
                body = " ".join(
                    f"Found {_MARKERS[i]} (source: {_SOURCES[i]})." for i in range(rounds)
                )
                return _llm_response(body)
            idx = len([c for c in calls if c.get("tool_choice") != "none"]) - 1
            return _llm_response(
                f"Now let me check source {idx}.",
                tool_calls=[{"id": f"c{idx}", "name": "web_search", "arguments": "{}"}],
            )

        client.respond = AsyncMock(side_effect=_respond)
        client.dialect_for_role = MagicMock(return_value=None)
        client.recorded_calls = calls
        return client

    @pytest.mark.asyncio
    async def test_report_names_every_marker_and_no_narration_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 3)
        client = self._client(rounds=3)
        dispatched = {"n": 0}

        async def _dispatch(**kwargs: Any) -> dict[str, Any]:
            content = _seeded_tool_content(dispatched["n"])
            dispatched["n"] += 1
            return _dispatch_result(kwargs["tool_call_id"], "web_search", content)

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("web_search"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(side_effect=_dispatch),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["web_search"]), llm_client=client, trace_id="t"
            )

        assert result.stop_reason == "cap"
        assert result.report_kind == "synthesized"
        for marker, source in zip(_MARKERS, _SOURCES, strict=True):
            assert marker in result.summary
            assert source in result.summary
        assert "Now let me check" not in result.summary

        synthesis_call = client.recorded_calls[-1]
        assert synthesis_call["tool_choice"] == "none"
        assert synthesis_call["tools"] is not None
        assert synthesis_call["tools"][0]["function"]["name"] == "web_search"

    @pytest.mark.asyncio
    async def test_seeded_negative_without_forced_synthesis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disable the mechanism and the criterion must fail (FRE-1482 AC-6).

        Forced synthesis is disabled by making the loop's cap unreachable within
        the stub's script, so the terminal content falls back to the round texts
        the way it did before ADR-0149. Zero markers, every narration line —
        which is exactly what the owner received on 2026-09-10.
        """
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 3)
        client = self._client(rounds=3)
        dispatched = {"n": 0}

        async def _dispatch(**kwargs: Any) -> dict[str, Any]:
            content = _seeded_tool_content(dispatched["n"])
            dispatched["n"] += 1
            return _dispatch_result(kwargs["tool_call_id"], "web_search", content)

        import personal_agent.orchestrator.sub_agent as sa

        async def _no_synthesis(*args: Any, **kwargs: Any) -> Any:
            state = args[0]
            return sa._ToolLoopOutcome(
                content="\n\n".join(state.round_texts),
                stated_tool_gap=None,
                stop_reason="cap",
                report_kind="narration",
            )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("web_search"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(side_effect=_dispatch),
            ),
            patch.object(sa, "_forced_synthesis", _no_synthesis),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["web_search"]), llm_client=client, trace_id="t"
            )

        assert all(marker not in result.summary for marker in _MARKERS)
        assert "Now let me check" in result.summary


class TestBudgetAndCountdown:
    """ADR-0149 AC-4 / AC-4a — the worker can see the budget it is spending."""

    @pytest.mark.asyncio
    async def test_budget_is_stated_in_the_task_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0150 D3 (revising ADR-0149 move 1): the number is in the task message.

        The system prompt keeps only how the budget works. The number varies per
        task by level, and in the system prompt it would split the type's prefix.
        """
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 7)
        client = AsyncMock()
        client.respond = AsyncMock(return_value=_llm_response("done"))

        await run_sub_agent(spec=_spec(), llm_client=client, trace_id="t")

        messages = client.respond.call_args.kwargs["messages"]
        assert messages[0]["role"] == "system"
        assert "Your task states your budget of tool rounds." in messages[0]["content"]
        assert "budget of 7" not in messages[0]["content"]
        assert "You have a budget of 7 tool round(s)." in messages[-1]["content"]

    @pytest.mark.asyncio
    async def test_the_bytes_are_identical_across_same_type_workers_and_turns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0150's revision of AC-4a: identical across the workers of one type.

        This is why the date and the budget live in the task message instead: either
        here would change these bytes per turn or per task, and with them the prefix.
        """
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 5)
        prompts: list[str] = []

        for spec in (
            _spec(task="worker one"),
            _spec_with_tools(["run_python"]),
            _spec(task="worker three"),
        ):
            client = AsyncMock()
            client.respond = AsyncMock(return_value=_llm_response("done"))
            await run_sub_agent(spec=spec, llm_client=client, trace_id="t")
            prompts.append(client.respond.call_args.kwargs["messages"][0]["content"])

        # Including the grant-less workers: a fan-out can mix granted and
        # grant-less tasks of one type, and the revision admits no variation.
        assert len(set(prompts)) == 1

    @pytest.mark.asyncio
    async def test_countdown_follows_every_round_and_precedes_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-4: move 3, after each round's tool results, at the tail only."""
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 3)
        # The loop mutates ONE message list in place, so call_args_list holds the
        # same object on every call and shows only the final state. Snapshot it.
        sent: list[list[dict[str, Any]]] = []
        scripted = [
            _llm_response("", tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}]),
            _llm_response("", tool_calls=[{"id": "c1", "name": "run_python", "arguments": "{}"}]),
            _llm_response("final"),
        ]

        async def _respond(**kwargs: Any) -> dict[str, Any]:
            sent.append([dict(m) for m in kwargs["messages"]])
            return scripted[len(sent) - 1]

        client = AsyncMock()
        client.dialect_for_role = MagicMock(return_value=None)
        client.respond = AsyncMock(side_effect=_respond)

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c", "run_python", "x" * 500)),
            ),
        ):
            await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=client, trace_id="t"
            )

        def _countdowns(messages: list[dict[str, Any]]) -> list[str]:
            return [
                str(m["content"])
                for m in messages
                if str(m.get("content")).startswith("Tool budget:")
            ]

        # None precedes the first round; one follows each round thereafter.
        assert _countdowns(sent[0]) == []
        assert len(_countdowns(sent[1])) == 1
        assert len(_countdowns(sent[2])) == 2

        first, second = _countdowns(sent[2])
        assert "Tool budget: 2 of 3 round(s) remaining." in first
        assert "Tool budget: 1 of 3 round(s) remaining." in second
        # The characters absorbed are reported too, because context growth is
        # what actually binds even though the enforced cap counts rounds.
        assert "Absorbed so far: 500 characters" in first
        assert "Absorbed so far: 1,000 characters" in second
        # Tail append: the countdown directly follows the round's tool results,
        # and nothing above it is rewritten (ADR-0081 D2).
        assert sent[1][-1]["content"] == first
        assert sent[1][-2]["role"] == "tool"
        assert sent[2][: len(sent[1])] == sent[1]


def _typed_spec(
    worker_type: WorkerType,
    thoroughness: Thoroughness,
    task: str = "test task",
    sibling_tasks: tuple[str, ...] = (),
) -> SubAgentSpec:
    return SubAgentSpec(
        task=task,
        context=[
            {"role": "user", "content": "pinned one"},
            {"role": "assistant", "content": "pinned two"},
            {"role": "user", "content": "pinned three"},
            {"role": "assistant", "content": "pinned four"},
        ],
        max_tokens=1024,
        timeout_seconds=30.0,
        tools=list(WORKER_TYPES[worker_type].tools),
        worker_type=worker_type,
        thoroughness=thoroughness,
        sibling_tasks=sibling_tasks,
    )


async def _first_call(spec: SubAgentSpec) -> dict[str, Any]:
    """Run one worker whose model stops at once, and return its first call's kwargs.

    Snapshots ``messages`` at call time (``list(...)``, a shallow copy): every
    call shares one mutable list (``_ToolLoopState.messages``), appended to in
    place, so reading ``call_args_list[0].kwargs["messages"]`` after the run
    completes would return whatever the list holds by then — for a
    schema-backed worker (ADR-0150 D1) that now includes the voluntary-stop
    landing call's own appended note, not just what the first call actually saw.
    """
    calls: list[dict[str, Any]] = []

    async def _respond(**kwargs: Any) -> dict[str, Any]:
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return _llm_response("done")

    client = AsyncMock()
    client.respond = AsyncMock(side_effect=_respond)
    await run_sub_agent(spec=spec, llm_client=client, trace_id="t")
    return calls[0]


class TestTypedPrefix:
    """FRE-1493 AC-1 (ADR-0150 AC-5), unit half: the bytes that make the cached prefix.

    The prefix a worker shares is the system message and the tools array (Qwen chat
    templates render tools inside the system region). The live
    ``cache_read_tokens >= 0.95 x P`` measurement needs the local backend and is
    master's post-deploy check; this pins the input that ratio depends on.
    """

    @pytest.fixture(autouse=True)
    def _levels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 5)
        monkeypatch.setattr(
            settings, "sub_agent_rounds_by_thoroughness", {"quick": 2, "thorough": 5}
        )

    @pytest.mark.asyncio
    async def test_same_type_workers_share_system_and_tools_bytes_across_levels(self) -> None:
        quick = await _first_call(
            _typed_spec(WorkerType.RESEARCHER, "quick", "find a", sibling_tasks=("find_b",))
        )
        thorough = await _first_call(
            _typed_spec(WorkerType.RESEARCHER, "thorough", "find b", sibling_tasks=("find_a",))
        )

        assert quick["messages"][0] == thorough["messages"][0]
        assert quick["tools"] == thorough["tools"]
        assert quick["tools"]
        # The context slice between them is identical too, so the shared prefix
        # runs up to the task message — the only per-worker part.
        assert quick["messages"][1:5] == thorough["messages"][1:5]
        assert quick["messages"][-1] != thorough["messages"][-1]

    @pytest.mark.asyncio
    async def test_type_boundary_changes_the_prefix(self) -> None:
        researcher = await _first_call(_typed_spec(WorkerType.RESEARCHER, "quick"))
        general = await _first_call(_typed_spec(WorkerType.GENERAL, "quick"))

        assert researcher["messages"][0] != general["messages"][0]
        assert researcher["tools"] != general["tools"]

    @pytest.mark.asyncio
    async def test_researcher_gets_its_block_and_general_gets_none(self) -> None:
        researcher = await _first_call(_typed_spec(WorkerType.RESEARCHER, "quick"))
        general = await _first_call(_typed_spec(WorkerType.GENERAL, "quick"))

        block = WORKER_TYPES[WorkerType.RESEARCHER].prompt_block
        assert block in researcher["messages"][0]["content"]
        assert "You research one bounded question" not in general["messages"][0]["content"]


class TestThoroughnessBinds:
    """FRE-1493 AC-2 (ADR-0150 AC-6): the level's budget binds the loop."""

    @pytest.mark.asyncio
    async def test_quick_task_spends_two_rounds_then_forced_synthesis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 5)
        monkeypatch.setattr(
            settings,
            "sub_agent_rounds_by_thoroughness",
            {"quick": 2, "standard": 4, "thorough": 5},
        )
        sent: list[list[dict[str, Any]]] = []
        call_kwargs: list[dict[str, Any]] = []

        async def _respond(**kwargs: Any) -> dict[str, Any]:
            sent.append([dict(m) for m in kwargs["messages"]])
            call_kwargs.append(kwargs)
            if kwargs.get("tool_choice") == "none" or not kwargs.get("tools"):
                return _llm_response("the report")
            n = len(sent)
            return _llm_response(
                "", tool_calls=[{"id": f"c{n}", "name": "run_python", "arguments": "{}"}]
            )

        client = AsyncMock()
        client.dialect_for_role = MagicMock(return_value=None)
        client.respond = AsyncMock(side_effect=_respond)
        dispatch = AsyncMock(return_value=_dispatch_result("c", "run_python", "x" * 10))

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch("personal_agent.orchestrator.sub_agent.dispatch_tool_call", dispatch),
        ):
            result = await run_sub_agent(
                spec=_typed_spec(WorkerType.GENERAL, "quick"), llm_client=client, trace_id="t"
            )

        task_message = sent[0][-1]["content"]
        assert "Thoroughness: quick. You have a budget of 2 tool round(s)." in task_message
        assert "budget of 5" not in task_message
        countdowns = [str(m["content"]) for m in sent[1] if str(m["content"]).startswith("Tool")]
        assert countdowns == [countdowns[0]]
        assert countdowns[0].startswith("Tool budget: 1 of 2 round(s) remaining.")
        # Two tool rounds, then the forced synthesis — never a third round.
        assert dispatch.await_count == 2
        assert len(sent) == 3
        assert result.stop_reason == "cap"
        assert result.tool_iterations == 2


class TestTaskMessage:
    """FRE-1493 AC-5 (ADR-0150 AC-9) and D5: what the worker is told beyond its task."""

    @pytest.mark.asyncio
    async def test_the_sibling_line_names_the_other_tasks(self) -> None:
        call = await _first_call(
            _typed_spec(WorkerType.RESEARCHER, "quick", sibling_tasks=("find_b", "find_c"))
        )
        assert (
            "Other workers in this turn own: find_b, find_c. Stay inside your task."
            in call["messages"][-1]["content"]
        )

    @pytest.mark.asyncio
    async def test_a_lone_worker_reads_none(self) -> None:
        call = await _first_call(_typed_spec(WorkerType.RESEARCHER, "quick"))
        assert (
            "Other workers in this turn own: none. Stay inside your task."
            in call["messages"][-1]["content"]
        )

    @pytest.mark.asyncio
    async def test_task_message_order_and_report_line(self) -> None:
        call = await _first_call(
            _typed_spec(WorkerType.GENERAL, "quick", task="add two numbers", sibling_tasks=("x",))
        )
        lines = call["messages"][-1]["content"].splitlines()
        assert lines[-4] == "Task: add two numbers"
        assert lines[-3].startswith("Other workers in this turn own: x.")
        assert lines[-2].startswith("Thoroughness: quick. You have a budget of ")
        assert lines[-1] == "Report in text."

    @pytest.mark.asyncio
    async def test_base_prompt_carries_the_d5_lines_verbatim(self) -> None:
        system = (await _first_call(_typed_spec(WorkerType.GENERAL, "quick")))["messages"][0][
            "content"
        ]
        for line in (
            "You are a focused sub-agent executing a specific sub-task. Do not ask follow-up "
            "questions.",
            "Do not invent missing facts, inputs, tool results, or assumptions. When the "
            "evidence is not there, say what you could not determine.",
            "Complete only the assigned task. Do not broaden its scope or solve the parent task.",
            "Only what you write in your report reaches the parent. Nothing else you read or "
            "did survives.",
            "(one tool name, no other text on that line).",
        ):
            assert line in system.splitlines() or line in system
        assert 'end your response with a final line reading exactly "TOOL_GAP: <tool_name>"' in (
            system
        )


class TestWorkerKnowsTheDate:
    """ADR-0149 AC-5 — move 2, the date travels a stated path into the task message."""

    @pytest.mark.asyncio
    async def test_task_message_carries_the_turn_timestamp(self) -> None:
        from datetime import datetime, timezone

        client = AsyncMock()
        client.respond = AsyncMock(return_value=_llm_response("done"))
        spec = replace(_spec(), turn_started_at=datetime(2026, 9, 10, 12, 17, tzinfo=timezone.utc))

        await run_sub_agent(spec=spec, llm_client=client, trace_id="t")

        task_message = client.respond.call_args.kwargs["messages"][-1]["content"]
        assert "## Current Date & Time" in task_message
        assert "Current date: 2026-09-10" in task_message
        # The date is NOT in the system prompt — that string must stay identical
        # across turns (AC-4a).
        system = client.respond.call_args.kwargs["messages"][0]["content"]
        assert "Current date" not in system

    @pytest.mark.asyncio
    async def test_no_timestamp_omits_the_block_and_warns(self) -> None:
        """Seeded negative for move 2, and the honest behaviour for a caller
        outside a turn: no date is invented from ``now``.
        """
        client = AsyncMock()
        client.respond = AsyncMock(return_value=_llm_response("done"))

        with structlog.testing.capture_logs() as logs:
            await run_sub_agent(spec=_spec(), llm_client=client, trace_id="t", session_id="s")

        task_message = client.respond.call_args.kwargs["messages"][-1]["content"]
        assert "## Current Date & Time" not in task_message
        warnings = [e for e in logs if e.get("event") == "sub_agent_no_turn_timestamp"]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"


class TestLandingReserve:
    """ADR-0149 AC-3 — the worker lands before it is cut."""

    @pytest.mark.asyncio
    async def test_reserve_stops_a_round_it_cannot_finish_and_writes_instead(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The third worker of a fan-out lands with fewer rounds rather than dying.

        ``max_deadline_seconds`` is the shrinking per-worker budget FRE-1397
        hands down. Set below ``mean_round_s + effective_timeout`` and the loop
        must not start another round.
        """
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 5)
        sent: list[dict[str, Any]] = []

        async def _respond(**kwargs: Any) -> dict[str, Any]:
            sent.append(kwargs)
            if kwargs.get("tool_choice") == "none":
                return _llm_response("Report from what I gathered.")
            return _llm_response(
                "searching",
                tool_calls=[{"id": f"c{len(sent)}", "name": "run_python", "arguments": "{}"}],
            )

        client = AsyncMock()
        client.respond = AsyncMock(side_effect=_respond)
        client.dialect_for_role = MagicMock(return_value=None)

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c", "run_python", "ok")),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"], timeout=10.0),
                llm_client=client,
                trace_id="t",
                # Below 10 (mean estimate) + 10 (budget), so the reserve fires
                # before any round starts.
                max_deadline_seconds=15.0,
            )

        assert result.stop_reason == "time_reserve"
        assert result.report_kind == "synthesized"
        assert result.tool_iterations == 0
        # Exactly one call, and it was the tools-off one.
        assert len(sent) == 1
        assert sent[0]["tool_choice"] == "none"
        assert "Your time budget is nearly spent." in str(sent[0]["messages"][-1]["content"])

    @pytest.mark.asyncio
    async def test_seeded_negative_ample_budget_runs_rounds_normally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With room to work, the reserve must not fire — or it measures nothing."""
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 2)
        client = AsyncMock()
        client.dialect_for_role = MagicMock(return_value=None)
        client.respond = AsyncMock(
            side_effect=[
                _llm_response(
                    "", tool_calls=[{"id": "c0", "name": "run_python", "arguments": "{}"}]
                ),
                _llm_response("done"),
            ]
        )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c", "run_python", "ok")),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["run_python"], timeout=10.0),
                llm_client=client,
                trace_id="t",
            )

        assert result.stop_reason == "completed"
        assert result.tool_iterations == 1


class TestTerminalPathsDeclareAReport:
    """ADR-0149 AC-2 — every terminal path yields a declared report, never silence."""

    @staticmethod
    def _two_rounds_then(raiser: Any) -> AsyncMock:
        """A client that completes two tool rounds and then fails the way given."""
        state = {"n": 0}

        async def _respond(**kwargs: Any) -> dict[str, Any]:
            state["n"] += 1
            if state["n"] <= 2:
                return _llm_response(
                    f"round {state['n']}",
                    tool_calls=[
                        {
                            "id": f"c{state['n']}",
                            "name": "web_search",
                            "arguments": '{"query": "mallorca events"}',
                        }
                    ],
                )
            raise raiser

        client = AsyncMock()
        client.respond = AsyncMock(side_effect=_respond)
        client.dialect_for_role = MagicMock(return_value=None)
        return client

    @staticmethod
    def _patches() -> Any:
        return (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("web_search"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c", "web_search", "x" * 1500)),
            ),
        )

    @pytest.mark.asyncio
    async def test_per_call_timeout_after_two_rounds_yields_a_ledger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FRE-1482 AC-3: this is the OVH failure — 6 searches, zero characters."""
        from personal_agent.config import settings
        from personal_agent.llm_client.types import LLMTimeout

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 5)
        client = self._two_rounds_then(LLMTimeout("Timeout passed=90.0"))
        layer_patch, dispatch_patch = self._patches()

        with layer_patch, dispatch_patch:
            result = await run_sub_agent(
                spec=_spec_with_tools(["web_search"], timeout=10.0),
                llm_client=client,
                trace_id="t",
            )

        assert result.stop_reason == "timeout"
        assert result.report_kind == "ledger"
        # The ledger lists BOTH completed rounds with their arguments and sizes.
        assert result.summary.count("web_search(") == 2
        assert "mallorca events" in result.summary
        assert "1,500 chars" in result.summary
        # Never zero characters again.
        assert len(result.full_output) > 0

    @pytest.mark.asyncio
    async def test_generic_exception_yields_a_ledger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 5)
        client = self._two_rounds_then(RuntimeError("upstream 503"))
        layer_patch, dispatch_patch = self._patches()

        with layer_patch, dispatch_patch:
            result = await run_sub_agent(
                spec=_spec_with_tools(["web_search"], timeout=10.0),
                llm_client=client,
                trace_id="t",
            )

        # The loop's own synthesis attempt is what catches this, so the stop
        # reason is the path that triggered it.
        assert result.report_kind == "ledger"
        assert result.summary.count("web_search(") == 2
        assert "upstream 503" in result.summary

    @pytest.mark.asyncio
    async def test_completed_with_empty_text_is_a_failed_landing(self) -> None:
        """ADR-0149 D3: empty content is not a report, however politely it stopped."""
        client = AsyncMock()
        client.respond = AsyncMock(return_value=_llm_response("   "))

        result = await run_sub_agent(spec=_spec(), llm_client=client, trace_id="t")

        assert result.stop_reason == "completed"
        assert result.report_kind == "ledger"
        assert result.success is False
        assert "the model returned no text" in result.summary

    @pytest.mark.asyncio
    async def test_cancellation_writes_the_capture_and_still_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0149 D3: the ledger goes to the capture; the cancellation is re-raised."""
        import personal_agent.orchestrator.sub_agent as sa
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 5)
        captured: list[Any] = []
        monkeypatch.setattr(sa, "write_sub_agent_capture", lambda cap: captured.append(cap))

        client = self._two_rounds_then(asyncio.CancelledError())
        layer_patch, dispatch_patch = self._patches()

        with layer_patch, dispatch_patch, pytest.raises(asyncio.CancelledError):
            await run_sub_agent(
                spec=_spec_with_tools(["web_search"], timeout=10.0),
                llm_client=client,
                trace_id="t",
            )

        assert len(captured) == 1
        assert captured[0].stop_reason == "cancelled"
        assert captured[0].report_kind == "ledger"
        assert captured[0].full_output.count("web_search(") == 2
        # The per-round record the raise decision reads (ADR-0149 D5).
        assert len(captured[0].rounds) == 2
        assert captured[0].rounds[0]["tool"] == "web_search"
        assert captured[0].rounds[0]["result_chars"] == 1500
        assert captured[0].rounds[0]["wall_s"] is not None


class TestFinishReasonOnCutLanding:
    """ADR-0150 D6 / AC-3 (FRE-1492) — a cut landing is never reported as complete."""

    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        import personal_agent.orchestrator.sub_agent as sa

        captured: list[Any] = []
        monkeypatch.setattr(sa, "write_sub_agent_capture", lambda cap: captured.append(cap))
        return captured

    @pytest.mark.asyncio
    async def test_cap_path_cut_landing_is_narration_not_synthesized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-1: the forced-synthesis call, cut at the ceiling, is narration, never synthesized.

        Checked before the content is trusted as a report.
        """
        from personal_agent.config import settings

        # Three rounds, like TestForcedSynthesisReportsEvidence — sub_agent_max_tool_iterations=1
        # trips the landing RESERVE before the first round ever runs (the reserve is sized
        # from `mean_round_s + effective_timeout`, which floors at two full budgets), so the
        # cap path needs at least this many rounds to actually reach the cap check.
        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 3)
        captured = self._capture(monkeypatch)

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response(
                    "", tool_calls=[{"id": f"c{i}", "name": "web_search", "arguments": "{}"}]
                )
                for i in range(3)
            ]
            + [_llm_response_with_finish_reason("Found ZORPTAL (source: brevik", "length")]
        )
        mock_client.dialect_for_role = MagicMock(return_value=None)

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("web_search"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c", "web_search", "ok")),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["web_search"]), llm_client=mock_client, trace_id="t"
            )

        assert result.stop_reason == "cap"
        assert result.report_kind == "narration"
        assert result.success is False
        assert result.finish_reason == "length"
        assert "Found ZORPTAL (source: brevik" in result.summary
        assert "cut off at the token ceiling" in result.summary
        # ADR-0150's own AC-3: the capture carries the same finish_reason.
        assert captured[-1].finish_reason == "length"

    @pytest.mark.asyncio
    async def test_completed_path_cut_landing_is_narration_not_synthesized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-1, repeated on the completed path: no tool calls, cut at the ceiling."""
        captured = self._capture(monkeypatch)

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            return_value=_llm_response_with_finish_reason("Here is a partial find", "length")
        )

        result = await run_sub_agent(spec=_spec(), llm_client=mock_client, trace_id="t")

        assert result.stop_reason == "completed"
        assert result.report_kind == "narration"
        assert result.success is False
        assert result.finish_reason == "length"
        assert "Here is a partial find" in result.summary
        assert "cut off at the token ceiling" in result.summary
        assert captured[-1].finish_reason == "length"

    @pytest.mark.asyncio
    async def test_unset_finish_reason_with_empty_content_still_yields_ledger(self) -> None:
        """AC-2, scoped per the plan doc's Scope decision.

        Adding the field must not disturb the pre-existing empty-content ledger path
        when finish_reason is unset.
        """
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value=_llm_response_with_finish_reason("   ", None))

        result = await run_sub_agent(spec=_spec(), llm_client=mock_client, trace_id="t")

        assert result.stop_reason == "completed"
        assert result.report_kind == "ledger"
        assert result.success is False
        assert result.finish_reason is None

    @pytest.mark.asyncio
    async def test_every_round_records_its_own_finish_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-3: rounds[] carries each round's own finish_reason.

        The terminal call carries its own value on SubAgentResult/SubAgentCapture.
        """
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 5)
        captured = self._capture(monkeypatch)

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response_with_finish_reason(
                    "",
                    "tool_calls",
                    tool_calls=[{"id": f"c{i}", "name": "web_search", "arguments": "{}"}],
                )
                for i in range(3)
            ]
            + [_llm_response_with_finish_reason("final report", "stop")]
        )

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("web_search"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c", "web_search", "ok")),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["web_search"]), llm_client=mock_client, trace_id="t"
            )

        assert result.finish_reason == "stop"
        assert result.report_kind == "synthesized"
        cap = captured[-1]
        assert len(cap.rounds) == 3
        assert all(r["finish_reason"] == "tool_calls" for r in cap.rounds)

    @pytest.mark.asyncio
    async def test_seeded_negative_without_the_length_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-5: disable the check and the cut cap-path reads as synthesized (wrong)."""
        import personal_agent.orchestrator.sub_agent as sa
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 3)
        monkeypatch.setattr(sa, "_extract_finish_reason", lambda response: None)

        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(
            side_effect=[
                _llm_response(
                    "", tool_calls=[{"id": f"c{i}", "name": "web_search", "arguments": "{}"}]
                )
                for i in range(3)
            ]
            + [_llm_response_with_finish_reason("Found ZORPTAL (source: brevik", "length")]
        )
        mock_client.dialect_for_role = MagicMock(return_value=None)

        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("web_search"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c", "web_search", "ok")),
            ),
        ):
            result = await run_sub_agent(
                spec=_spec_with_tools(["web_search"]), llm_client=mock_client, trace_id="t"
            )

        assert result.report_kind == "synthesized"


class TestSynthesisWireForm:
    """ADR-0149 AC-6 — the declared form is the form actually sent."""

    @staticmethod
    def _capped_client(dialect: Any) -> AsyncMock:
        calls: list[dict[str, Any]] = []

        async def _respond(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            if kwargs.get("tools") is None or kwargs.get("tool_choice") == "none":
                return _llm_response("the report")
            return _llm_response(
                "", tool_calls=[{"id": "c", "name": "run_python", "arguments": "{}"}]
            )

        client = AsyncMock()
        client.respond = AsyncMock(side_effect=_respond)
        client.dialect_for_role = MagicMock(return_value=dialect)
        client.provider = "slm_local"
        client.recorded_calls = calls
        return client

    @staticmethod
    async def _run(client: AsyncMock) -> Any:
        with (
            patch(
                "personal_agent.orchestrator.sub_agent.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("run_python"),
            ),
            patch(
                "personal_agent.orchestrator.sub_agent.dispatch_tool_call",
                AsyncMock(return_value=_dispatch_result("c", "run_python", "ok")),
            ),
        ):
            return await run_sub_agent(
                spec=_spec_with_tools(["run_python"]), llm_client=client, trace_id="t"
            )

    @pytest.mark.asyncio
    async def test_declared_true_keeps_the_tools_and_pins_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cache-preserving form: the array stays, the choice forbids calling."""
        from personal_agent.config import settings
        from personal_agent.llm_client.models import Dialect

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 1)
        client = self._capped_client(Dialect.LLAMACPP_QWEN)

        result = await self._run(client)

        synthesis = client.recorded_calls[-1]
        assert synthesis["tools"] is not None
        assert synthesis["tool_choice"] == "none"
        assert result.report_kind == "synthesized"

    @pytest.mark.asyncio
    async def test_declared_false_drops_the_tools_and_logs_the_miss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seeded negative for D6: patch the declaration and the wire form changes."""
        import personal_agent.llm_client.models as models
        from personal_agent.config import settings
        from personal_agent.llm_client.models import Dialect

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 1)
        monkeypatch.setattr(
            models,
            "SYNTHESIS_RETAINS_TOOLS",
            {**models.SYNTHESIS_RETAINS_TOOLS, Dialect.LLAMACPP_QWEN: False},
        )
        client = self._capped_client(Dialect.LLAMACPP_QWEN)

        with structlog.testing.capture_logs() as logs:
            await self._run(client)

        synthesis = client.recorded_calls[-1]
        assert synthesis["tools"] is None
        assert synthesis["tool_choice"] is None
        misses = [e for e in logs if e.get("event") == "forced_synthesis_cache_miss_declared"]
        assert len(misses) == 1
        assert misses[0]["log_level"] == "warning"

    @pytest.mark.asyncio
    async def test_a_runtime_rejection_is_one_attempt_and_a_ledger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provider that refuses the declared form is a config defect, not a retry.

        ADR-0149 D6: there is no drop-tools fallback. The worker recovers from
        its own limits; it does not retry the world.
        """
        from personal_agent.config import settings
        from personal_agent.llm_client.models import Dialect

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 1)
        calls: list[dict[str, Any]] = []

        async def _respond(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            if kwargs.get("tool_choice") == "none":
                raise ValueError("tool_choice 'none' is not supported")
            return _llm_response(
                "", tool_calls=[{"id": "c", "name": "run_python", "arguments": "{}"}]
            )

        client = AsyncMock()
        client.respond = AsyncMock(side_effect=_respond)
        client.dialect_for_role = MagicMock(return_value=Dialect.OVH_QWEN)
        client.provider = "ovhcloud"

        result = await self._run(client)

        assert result.report_kind == "ledger"
        assert len([c for c in calls if c.get("tool_choice") == "none"]) == 1
        assert "ovhcloud" in result.summary
        assert "ovh_qwen" in result.summary

    @pytest.mark.asyncio
    async def test_a_client_without_the_seam_still_keeps_its_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defensive: `llm_client` is Any here and not always a LiteLLMClient.

        An AsyncMock answers every attribute with a coroutine factory. Handing
        that to the dialect table would raise a KeyError out of the worker,
        turning a capped worker into a crashed one.
        """
        from personal_agent.config import settings

        monkeypatch.setattr(settings, "sub_agent_max_tool_iterations", 1)
        client = self._capped_client(None)
        del client.dialect_for_role

        result = await self._run(client)

        synthesis = client.recorded_calls[-1]
        assert synthesis["tool_choice"] == "none"
        assert result.report_kind == "synthesized"
