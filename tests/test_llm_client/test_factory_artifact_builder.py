"""AC-8 regression guard (ADR-0119, FRE-879).

A local-profile artifact build must stay on the local model, never silently cross to
cloud Haiku. This exercises get_llm_client's real ExecutionProfile-based dispatch (no
mocking of the resolution seam itself) — the direct proof that config/model_roles.yaml's
matrix plays no part in resolving artifact_builder.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from personal_agent.config.profile import (
    ExecutionProfile,
    _current_profile,
    set_current_profile,
)
from personal_agent.llm_client.client import LocalLLMClient
from personal_agent.llm_client.factory import get_llm_client
from personal_agent.llm_client.litellm_client import LiteLLMClient


@pytest.fixture(autouse=True)
def _reset_profile() -> Iterator[None]:
    """Ensure no active profile leaks between tests in this module."""
    token = _current_profile.set(None)
    try:
        yield
    finally:
        _current_profile.reset(token)


class TestArtifactBuilderProfileDispatch:
    """AC-8: local profile -> LocalLLMClient; cloud profile -> LiteLLMClient(Haiku)."""

    def test_local_profile_resolves_artifact_builder_to_local_client(self) -> None:
        """A local ExecutionProfile dispatches artifact_builder to LocalLLMClient."""
        profile = ExecutionProfile(
            name="local",
            primary_model="primary",
            sub_agent_model="sub_agent",
            artifact_builder_model="sub_agent",
            provider_type="local",
        )
        set_current_profile(profile)

        client = get_llm_client(role_name="artifact_builder")

        assert isinstance(client, LocalLLMClient)

    def test_cloud_profile_resolves_artifact_builder_to_cloud_haiku_client(self) -> None:
        """A cloud ExecutionProfile dispatches artifact_builder to LiteLLMClient(Haiku)."""
        profile = ExecutionProfile(
            name="cloud",
            primary_model="claude_sonnet",
            sub_agent_model="claude_haiku",
            artifact_builder_model="claude_haiku",
            provider_type="cloud",
        )
        set_current_profile(profile)

        client = get_llm_client(role_name="artifact_builder")

        assert isinstance(client, LiteLLMClient)
        assert client.budget_role == "artifact_builder"

        from personal_agent.config import load_model_config

        expected_model_id = load_model_config().models["claude_haiku"].id
        assert client.model_id == expected_model_id
