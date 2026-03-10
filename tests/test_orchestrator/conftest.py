"""Conftest for orchestrator tests.

Prevents reflection from calling the live reasoning model in unit tests.
The reflection path (executor → generate_reflection_entry → reflection_dspy →
ModelRole.REASONING) creates its own LocalLLMClient internally, bypassing any
executor-level patches. This fixture blocks that path for all non-integration tests.
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def mock_reflection_for_unit_tests(request: pytest.FixtureRequest) -> None:
    """Block reflection LLM calls in non-integration orchestrator tests.

    Integration tests (marked with @pytest.mark.integration) run reflection
    against the real LLM server as intended.
    """
    if request.node.get_closest_marker("integration"):
        yield
        return

    with patch(
        "personal_agent.captains_log.reflection.generate_reflection_entry",
        new_callable=AsyncMock,
    ):
        yield
