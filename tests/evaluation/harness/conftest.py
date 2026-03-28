"""Pytest fixtures for evaluation harness.

Provides httpx client, ES client, Neo4j checker, and agent health check.
Tests marked with @pytest.mark.evaluation require the live agent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from tests.evaluation.harness.neo4j_checker import Neo4jChecker
from tests.evaluation.harness.runner import EvaluationRunner
from tests.evaluation.harness.telemetry import TelemetryChecker


@pytest.fixture(scope="session")
def telemetry_checker() -> TelemetryChecker:
    """Shared TelemetryChecker instance."""
    return TelemetryChecker()


@pytest_asyncio.fixture(scope="session")
async def neo4j_checker() -> AsyncIterator[Neo4jChecker | None]:
    """Shared Neo4jChecker instance. Yields None if Neo4j is unreachable."""
    checker = Neo4jChecker()
    connected = await checker.connect()
    if connected:
        yield checker
        await checker.disconnect()
    else:
        yield None


@pytest.fixture(scope="session")
def evaluation_runner(
    telemetry_checker: TelemetryChecker,
    neo4j_checker: Neo4jChecker | None,
) -> EvaluationRunner:
    """Shared EvaluationRunner instance."""
    return EvaluationRunner(telemetry=telemetry_checker, neo4j_checker=neo4j_checker)


@pytest_asyncio.fixture(scope="session")
async def agent_healthy(evaluation_runner: EvaluationRunner) -> None:
    """Skip all evaluation tests if the agent service is not running on port 9000."""
    healthy = await evaluation_runner.check_agent_health()
    if not healthy:
        pytest.skip("Agent service not running on port 9000")
