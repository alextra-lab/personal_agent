"""Tests for the expansion controller.

Tests the enforced expansion path: planner → validate → dispatch → synthesize.
Uses mocked LLM client and sub-agent runner.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.orchestrator.expansion_controller import (
    ExpansionController,
    _validate_plan_json,
)
from personal_agent.orchestrator.sub_agent_types import SubAgentResult


def _make_plan_json(tasks: int = 3) -> str:
    """Create valid plan JSON for testing."""
    plan = {
        "strategy": "HYBRID",
        "tasks": [
            {
                "name": f"task_{i}",
                "goal": f"Goal for task {i}",
                "constraints": [f"constraint_{i}"],
                "expected_output": "text",
            }
            for i in range(tasks)
        ],
    }
    return json.dumps(plan)


def _make_sub_agent_result(
    task_name: str = "task_0",
    success: bool = True,
    summary: str = "Result summary",
) -> SubAgentResult:
    return SubAgentResult(
        task_id=f"sub-{task_name}",
        spec_task=task_name,
        summary=summary,
        full_output=summary,
        tools_used=[],
        token_count=50,
        duration_ms=2000,
        success=success,
        error=None if success else "Timeout",
    )


class TestValidatePlanJson:
    def test_valid_plan(self) -> None:
        plan = _validate_plan_json(_make_plan_json(3))
        assert plan is not None
        assert len(plan.tasks) == 3
        assert plan.strategy == "HYBRID"

    def test_invalid_json(self) -> None:
        assert _validate_plan_json("not json") is None

    def test_missing_tasks(self) -> None:
        assert _validate_plan_json('{"strategy": "HYBRID"}') is None

    def test_empty_tasks(self) -> None:
        assert _validate_plan_json('{"strategy": "HYBRID", "tasks": []}') is None

    def test_task_missing_name(self) -> None:
        bad = '{"strategy": "HYBRID", "tasks": [{"goal": "g"}]}'
        assert _validate_plan_json(bad) is None

    def test_task_missing_goal(self) -> None:
        bad = '{"strategy": "HYBRID", "tasks": [{"name": "n"}]}'
        assert _validate_plan_json(bad) is None

    def test_caps_task_count_hybrid(self) -> None:
        plan = _validate_plan_json(_make_plan_json(10))
        # HYBRID caps at 4+1 = 5
        assert plan is not None
        assert len(plan.tasks) <= 5


class TestExpansionControllerExecute:
    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @pytest.fixture
    def mock_llm(self) -> AsyncMock:
        client = AsyncMock()
        client.respond = AsyncMock(return_value=_make_plan_json(3))
        return client

    @pytest.mark.asyncio
    async def test_successful_expansion(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """LLM produces valid plan → sub-agents execute → synthesis."""
        mock_results = [_make_sub_agent_result(f"task_{i}") for i in range(3)]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[{"role": "user", "content": "Compare Redis, Memcached, and Hazelcast"}],
            )

        assert result.plan is not None
        assert len(result.sub_agent_results) == 3
        assert all(r.success for r in result.sub_agent_results)

    @pytest.mark.asyncio
    async def test_hybrid_emits_start_and_complete_telemetry(
        self,
        controller: ExpansionController,
        mock_llm: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """HYBRID expansion emits hybrid_expansion_start and hybrid_expansion_complete (eval contract)."""
        caplog.set_level("INFO", logger="personal_agent.orchestrator.expansion_controller")
        mock_results = [_make_sub_agent_result(f"task_{i}") for i in range(3)]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace-hybrid-events",
                messages=[{"role": "user", "content": "Compare Redis, Memcached, and Hazelcast"}],
            )

        assert "hybrid_expansion_start" in caplog.text
        assert "hybrid_expansion_complete" in caplog.text

    @pytest.mark.asyncio
    async def test_fallback_on_invalid_plan(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """LLM produces garbage → fallback planner engaged."""
        mock_llm.respond = AsyncMock(return_value="I'll just answer directly...")

        # Fallback planner for "Compare Redis and Memcached" (vs pattern) yields 3 tasks
        # (evaluate_redis, evaluate_memcached, synthesize_recommendation). Supply enough
        # mocks to cover any fallback plan size.
        mock_results = [
            _make_sub_agent_result("evaluate_redis"),
            _make_sub_agent_result("evaluate_memcached"),
            _make_sub_agent_result("synthesize_recommendation"),
        ]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis and Memcached",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[],
            )

        assert result.plan is not None
        assert result.plan.is_fallback is True

    @pytest.mark.asyncio
    async def test_planner_timeout_triggers_fallback(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """LLM planner times out → fallback planner engaged."""

        async def slow_respond(*args: Any, **kwargs: Any) -> str:
            await asyncio.sleep(100)
            return _make_plan_json()

        mock_llm.respond = slow_respond

        # Fallback planner for open-ended query yields 2 tasks (research + synthesis).
        # Supply enough mocks to cover both tasks.
        mock_results = [
            _make_sub_agent_result("research_analysis"),
            _make_sub_agent_result("synthesize_recommendation"),
        ]

        # Build a mock settings object with a very short planner timeout
        mock_settings = MagicMock()
        mock_settings.planner_timeout_seconds = 0.01
        mock_settings.worker_timeout_seconds = 45.0
        mock_settings.worker_global_timeout_seconds = 90.0
        mock_settings.sub_agent_max_tokens = 4096

        with (
            patch(
                "personal_agent.orchestrator.expansion_controller.run_sub_agent",
                side_effect=mock_results,
            ),
            patch(
                "personal_agent.orchestrator.expansion_controller.get_settings",
                return_value=mock_settings,
            ),
        ):
            result = await controller.execute(
                query="Research scaling approaches",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[],
            )

        assert result.plan is not None
        assert result.plan.is_fallback is True

    @pytest.mark.asyncio
    async def test_partial_sub_agent_failure(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """Some sub-agents fail → partial results returned."""
        mock_results = [
            _make_sub_agent_result("task_0", success=True),
            _make_sub_agent_result("task_1", success=False),
            _make_sub_agent_result("task_2", success=True),
        ]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[],
            )

        assert len(result.sub_agent_results) == 3
        assert result.successful_count == 2
        assert result.failed_count == 1


class TestGracefulDegradation:
    @pytest.fixture
    def controller(self) -> ExpansionController:
        return ExpansionController()

    @pytest.fixture
    def mock_llm(self) -> AsyncMock:
        client = AsyncMock()
        client.respond = AsyncMock(return_value=_make_plan_json(3))
        return client

    @pytest.mark.asyncio
    async def test_all_subagents_fail_degraded_response(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """All sub-agents fail → degraded=True."""
        mock_results = [
            _make_sub_agent_result(f"task_{i}", success=False)
            for i in range(3)
        ]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[],
            )

        assert result.degraded is True
        assert result.failed_count == 3

    @pytest.mark.asyncio
    async def test_synthesis_context_notes_failures(
        self, controller: ExpansionController, mock_llm: AsyncMock
    ) -> None:
        """Partial failure → synthesis context includes failure notes."""
        mock_results = [
            _make_sub_agent_result("task_0", success=True, summary="Redis is fast"),
            _make_sub_agent_result("task_1", success=False),
            _make_sub_agent_result("task_2", success=True, summary="Hazelcast scales"),
        ]

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=mock_results,
        ):
            result = await controller.execute(
                query="Compare Redis, Memcached, and Hazelcast",
                strategy="HYBRID",
                llm_client=mock_llm,
                trace_id="test-trace",
                messages=[],
            )

        assert "FAILED" in result.synthesis_context
        assert "Redis is fast" in result.synthesis_context
        assert "Hazelcast scales" in result.synthesis_context
