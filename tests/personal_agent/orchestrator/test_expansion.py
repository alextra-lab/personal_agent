"""Tests for HYBRID expansion orchestration."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import structlog.testing

from personal_agent.orchestrator.expansion import execute_hybrid, parse_decomposition_plan
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec


def _make_result(spec_task: str) -> SubAgentResult:
    from uuid import uuid4

    return SubAgentResult(
        task_id=uuid4(),
        spec_task=spec_task,
        summary="done",
        full_output="done",
        tools_used=[],
        token_count=10,
        duration_ms=1,
        success=True,
    )


class TestParseDecompositionPlan:
    def test_parses_numbered_tasks(self) -> None:
        plan = (
            "1. Research Graphiti temporal model\n"
            "2. Summarize current Neo4j approach\n"
            "3. Compare cost characteristics\n"
        )
        specs = parse_decomposition_plan(plan, max_sub_agents=3)
        assert len(specs) == 3
        assert "Graphiti" in specs[0].task
        assert "Neo4j" in specs[1].task

    def test_respects_max_sub_agents(self) -> None:
        plan = "1. A\n2. B\n3. C\n4. D\n5. E\n"
        specs = parse_decomposition_plan(plan, max_sub_agents=2)
        assert len(specs) == 2

    def test_empty_plan_returns_empty(self) -> None:
        specs = parse_decomposition_plan("", max_sub_agents=3)
        assert specs == []

    def test_specs_have_default_params(self) -> None:
        plan = "1. Do something\n"
        specs = parse_decomposition_plan(plan, max_sub_agents=3)
        assert specs[0].max_tokens > 0
        assert specs[0].timeout_seconds > 0
        assert specs[0].output_format == "markdown_summary"


class TestExecuteHybrid:
    @pytest.mark.asyncio
    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_runs_sub_agents_and_returns_results(
        self, mock_get_llm_client: AsyncMock
    ) -> None:
        mock_client = AsyncMock()
        mock_client.respond = AsyncMock(return_value="Sub-agent result text")
        mock_get_llm_client.return_value = mock_client

        specs = [
            SubAgentSpec(
                task="Research topic A",
                context=[],
                output_format="text",
                max_tokens=1024,
                timeout_seconds=30.0,
            ),
            SubAgentSpec(
                task="Research topic B",
                context=[],
                output_format="text",
                max_tokens=1024,
                timeout_seconds=30.0,
            ),
        ]

        results = await execute_hybrid(
            specs=specs,
            trace_id="test",
        )
        assert len(results) == 2
        assert all(isinstance(r, SubAgentResult) for r in results)
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_partial_failure_returns_all_results(
        self, mock_get_llm_client: AsyncMock
    ) -> None:
        call_count = 0

        async def flaky_respond(*args: object, **kwargs: object) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("LLM overloaded")
            return "success"

        mock_client = AsyncMock()
        mock_client.respond = flaky_respond
        mock_get_llm_client.return_value = mock_client

        specs = [
            SubAgentSpec(
                task=f"Task {i}",
                context=[],
                output_format="text",
                max_tokens=512,
                timeout_seconds=10.0,
            )
            for i in range(2)
        ]

        results = await execute_hybrid(
            specs=specs,
            trace_id="test",
        )
        assert len(results) == 2
        failures = [r for r in results if not r.success]
        successes = [r for r in results if r.success]
        assert len(failures) == 1
        assert len(successes) == 1

    @pytest.mark.asyncio
    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_tasks_run_sequentially_not_concurrently(
        self, mock_get_llm_client: AsyncMock
    ) -> None:
        """AC-1 — proven by real recorded timestamps, not by reading the loop."""
        mock_get_llm_client.return_value = AsyncMock()

        observed: list[tuple[float, float]] = []

        async def _timed_run(**kwargs: Any) -> SubAgentResult:
            observed_start = time.monotonic()
            await asyncio.sleep(0.02)
            observed.append((observed_start, time.monotonic()))
            return _make_result(kwargs["spec"].task)

        specs = [SubAgentSpec(task=f"Task {i}", context=[], timeout_seconds=10.0) for i in range(3)]

        with (
            patch(
                "personal_agent.orchestrator.expansion.run_sub_agent",
                side_effect=_timed_run,
            ),
            structlog.testing.capture_logs() as logs,
        ):
            results = await execute_hybrid(specs=specs, trace_id="test-sequential")

        assert len(results) == 3
        for earlier, later in zip(observed, observed[1:], strict=False):
            assert later[0] >= earlier[1]

        interval_events = [log for log in logs if log["event"] == "hybrid_expansion_intervals"]
        assert len(interval_events) == 1
        intervals = interval_events[0]["intervals"]
        assert len(intervals) == 3
        for earlier, later in zip(intervals, intervals[1:], strict=False):
            assert later["start_s"] >= earlier["end_s"]

    @pytest.mark.asyncio
    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_max_observed_concurrency_is_one(self, mock_get_llm_client: AsyncMock) -> None:
        """AC-1, belt-and-braces — never more than one sub-agent in flight."""
        mock_get_llm_client.return_value = AsyncMock()

        state = {"concurrent": 0}
        observed: list[int] = []

        async def _run(**kwargs: Any) -> SubAgentResult:
            state["concurrent"] += 1
            observed.append(state["concurrent"])
            await asyncio.sleep(0.01)
            state["concurrent"] -= 1
            return _make_result(kwargs["spec"].task)

        specs = [SubAgentSpec(task=f"Task {i}", context=[], timeout_seconds=10.0) for i in range(3)]

        with patch("personal_agent.orchestrator.expansion.run_sub_agent", side_effect=_run):
            await execute_hybrid(specs=specs, trace_id="test-max-concurrency")

        assert max(observed) == 1

    @pytest.mark.asyncio
    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_intervals_logged_for_every_spec(self, mock_get_llm_client: AsyncMock) -> None:
        """AC-4 — real-timestamp interval evidence, matching FRE-1380's instrumentation."""
        mock_get_llm_client.return_value = AsyncMock()

        specs = [SubAgentSpec(task=f"Task {i}", context=[], timeout_seconds=10.0) for i in range(3)]

        with (
            patch(
                "personal_agent.orchestrator.expansion.run_sub_agent",
                side_effect=lambda **kwargs: _make_result(kwargs["spec"].task),
            ),
            structlog.testing.capture_logs() as logs,
        ):
            await execute_hybrid(specs=specs, trace_id="test-intervals")

        interval_events = [log for log in logs if log["event"] == "hybrid_expansion_intervals"]
        assert len(interval_events) == 1
        intervals = interval_events[0]["intervals"]
        assert [iv["task"] for iv in intervals] == [s.task for s in specs]

    @pytest.mark.asyncio
    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_raw_exception_mid_batch_does_not_abort_loop(
        self, mock_get_llm_client: AsyncMock
    ) -> None:
        """AC-2 (strengthened) — a raw exception (not a caught-internal failure
        converted to a SubAgentResult) from one task does not stop later tasks,
        and every task's interval is still recorded (the `finally` guarantee).
        """
        mock_get_llm_client.return_value = AsyncMock()

        specs = [SubAgentSpec(task=f"Task {i}", context=[], timeout_seconds=10.0) for i in range(3)]

        async def _run(**kwargs: Any) -> SubAgentResult:
            if kwargs["spec"].task == "Task 1":
                raise RuntimeError("boom before run_sub_agent's own try block")
            return _make_result(kwargs["spec"].task)

        with (
            patch("personal_agent.orchestrator.expansion.run_sub_agent", side_effect=_run),
            structlog.testing.capture_logs() as logs,
        ):
            results = await execute_hybrid(specs=specs, trace_id="test-raw-exception")

        assert len(results) == 2  # the raising task is filtered, not a SubAgentResult
        assert {r.spec_task for r in results} == {"Task 0", "Task 2"}

        interval_events = [log for log in logs if log["event"] == "hybrid_expansion_intervals"]
        intervals = interval_events[0]["intervals"]
        assert len(intervals) == 3
        assert [iv["task"] for iv in intervals] == [s.task for s in specs]

    @pytest.mark.asyncio
    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_many_tasks_all_succeed(self, mock_get_llm_client: AsyncMock) -> None:
        """Regression — removing the semaphore introduces no other implicit ceiling."""
        mock_get_llm_client.return_value = AsyncMock()

        specs = [SubAgentSpec(task=f"Task {i}", context=[], timeout_seconds=10.0) for i in range(8)]

        with patch(
            "personal_agent.orchestrator.expansion.run_sub_agent",
            side_effect=lambda **kwargs: _make_result(kwargs["spec"].task),
        ):
            results = await execute_hybrid(specs=specs, trace_id="test-many-tasks")

        assert len(results) == 8
        assert all(r.success for r in results)
