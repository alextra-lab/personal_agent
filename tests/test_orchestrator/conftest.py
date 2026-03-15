"""Conftest for orchestrator tests.

Prevents reflection from calling the live reasoning model in unit tests.
The reflection path (executor → generate_reflection_entry → reflection_dspy →
ModelRole.REASONING) creates its own LocalLLMClient internally, bypassing any
executor-level patches. This fixture blocks that path for all non-integration tests.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.llm_client.models import ToolCallingStrategy


def configure_mock_llm_client_model_configs(mock_client: AsyncMock) -> None:
    """Set model_configs on a mock LocalLLMClient so executor can read effective_tool_strategy.

    Without this, model_configs.get(role) returns an AsyncMock and accessing
    .effective_tool_strategy raises AttributeError (coroutine has no attribute).
    """
    mock_def = MagicMock()
    mock_def.effective_tool_strategy = ToolCallingStrategy.NATIVE
    mock_client.model_configs = {
        "router": mock_def,
        "standard": mock_def,
        "reasoning": mock_def,
        "coding": mock_def,
    }


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
