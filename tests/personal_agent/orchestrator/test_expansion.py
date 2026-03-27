"""Tests for HYBRID expansion orchestration."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.orchestrator.expansion import execute_hybrid, parse_decomposition_plan
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec


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
            max_concurrent=2,
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
            max_concurrent=2,
        )
        assert len(results) == 2
        failures = [r for r in results if not r.success]
        successes = [r for r in results if r.success]
        assert len(failures) == 1
        assert len(successes) == 1

    @pytest.mark.asyncio
    @patch("personal_agent.llm_client.factory.get_llm_client")
    async def test_respects_max_concurrent(self, mock_get_llm_client: AsyncMock) -> None:
        concurrent_count = 0
        max_observed = 0

        async def tracking_respond(*args: object, **kwargs: object) -> str:
            nonlocal concurrent_count, max_observed
            concurrent_count += 1
            max_observed = max(max_observed, concurrent_count)
            await asyncio.sleep(0.05)
            concurrent_count -= 1
            return "done"

        mock_client = AsyncMock()
        mock_client.respond = tracking_respond
        mock_get_llm_client.return_value = mock_client

        specs = [
            SubAgentSpec(
                task=f"Task {i}",
                context=[],
                output_format="text",
                max_tokens=512,
                timeout_seconds=10.0,
            )
            for i in range(4)
        ]

        await execute_hybrid(
            specs=specs,
            trace_id="test",
            max_concurrent=1,
        )
        assert max_observed <= 1
