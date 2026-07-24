"""FRE-958 regression guard.

The ``sub_agent`` role is pinned to ``claude_sonnet`` (ADR-0121 T5, FRE-920,
2026-07-20) — a cloud/Anthropic deployment. A sub-agent spawn for this role
must build a client pointed at its provider (``LiteLLMClient``), never fall
through to the local SLM base URL (the FRE-958 bug: the executor built a
PRIMARY-role client and handed it to the sub-agent dispatch path instead).
"""

from __future__ import annotations

from personal_agent.llm_client.factory import get_llm_client
from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.types import ModelRole


class TestSubAgentResolution:
    """sub_agent resolves to its pinned cloud binding, never a local client."""

    def test_resolves_to_claude_sonnet_cloud_client(self) -> None:
        """sub_agent dispatches to LiteLLMClient(Sonnet) — the binding default."""
        client = get_llm_client(role_name=ModelRole.SUB_AGENT.value)

        assert isinstance(client, LiteLLMClient)
        assert client.provider == "anthropic"
        assert client.budget_role == "main_inference"

        from personal_agent.config import load_model_config

        expected_model_id = load_model_config().models["claude_sonnet"].id
        assert client.model_id == expected_model_id
