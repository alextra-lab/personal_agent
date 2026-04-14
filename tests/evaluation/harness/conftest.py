"""Pytest fixtures for evaluation harness.

Provides httpx client, ES client, and agent health check.
Tests marked with @pytest.mark.evaluation require the live agent.

Note:
    Neo4j post-path assertions are skipped — Neo4jChecker was archived with the
    Graphiti experiment (EVAL-02). See tests/archive/graphiti_experiment/ to restore.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from tests.evaluation.harness.runner import EvaluationRunner
from tests.evaluation.harness.telemetry import TelemetryChecker


@pytest.fixture(scope="session")
def telemetry_checker() -> TelemetryChecker:
    """Shared TelemetryChecker instance."""
    return TelemetryChecker()


@pytest.fixture(scope="session")
def evaluation_runner(
    telemetry_checker: TelemetryChecker,
) -> EvaluationRunner:
    """Shared EvaluationRunner instance."""
    return EvaluationRunner(telemetry=telemetry_checker)


@pytest_asyncio.fixture(scope="session")
async def agent_healthy(evaluation_runner: EvaluationRunner) -> None:
    """Skip all evaluation tests if the agent service is not running on port 9000."""
    healthy = await evaluation_runner.check_agent_health()
    if not healthy:
        pytest.skip("Agent service not running on port 9000")
