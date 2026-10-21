"""FRE-1647 — which deployments can force a tool, read from litellm's own map.

Offline: ``LITELLM_LOCAL_MODEL_COST_MAP`` is in force (``tests/conftest.py``). litellm
1.106.0 flags ``claude-sonnet-5-5`` with ``supports_forced_tool_use: false`` and refuses a
forced ``tool_choice`` for it client-side. Every other catalog deployment either says
``true`` or says nothing, and both keep today's forced request.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.config import load_model_config
from personal_agent.llm_client.factory import get_llm_client_for_key
from personal_agent.llm_client.forced_tool import can_force_tool
from personal_agent.llm_client.models import Placement
from personal_agent.llm_client.pricing import register_model_pricing


def _local_key() -> str:
    config = load_model_config()
    return next(
        key
        for key, definition in config.models.items()
        if definition.kind.value == "llm" and config.placement_of(key) is Placement.LOCAL
    )


class TestTheClientReadsTheFlag:
    """The client answers for its own deployment, so a ``--model-key`` override counts."""

    def test_sonnet_5_5_cannot_force_a_tool(self) -> None:
        client = get_llm_client_for_key("claude_sonnet_5_5", budget_role="captains_log")
        assert client.supports_forced_tool_use is False

    @pytest.mark.parametrize(
        "key", ["claude_sonnet", "claude_haiku_5_5", "gpt-6.1-sol", "gpt-6-luna", "qwen3.8-27b-ovh"]
    )
    def test_other_cloud_deployments_can(self, key: str) -> None:
        client = get_llm_client_for_key(key, budget_role="captains_log")
        assert client.supports_forced_tool_use is True

    def test_a_local_deployment_unknown_to_litellm_can(self) -> None:
        """No record is not a refusal: an unknown model keeps today's forced request."""
        client = get_llm_client_for_key(_local_key(), budget_role="captains_log")
        assert client.supports_forced_tool_use is True

    def test_another_providers_flag_on_the_same_bare_id_is_not_inherited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare-id entry is read only for the client's own provider (Codex review)."""
        import litellm

        client = get_llm_client_for_key("gpt-6-luna", budget_role="captains_log")
        monkeypatch.setitem(
            litellm.model_cost,
            client.model_id,
            {"litellm_provider": "anthropic", "supports_forced_tool_use": False},
        )
        assert client.supports_forced_tool_use is True

    def test_the_flag_survives_price_registration(self) -> None:
        """Startup registers catalog prices into the same map; the flag must not be lost."""
        register_model_pricing(load_model_config(Path("config/models.yaml")))
        client = get_llm_client_for_key("claude_sonnet_5_5", budget_role="captains_log")
        assert client.supports_forced_tool_use is False


class TestCanForceTool:
    """The producers' question, asked of any client."""

    def test_a_client_that_does_not_say_keeps_the_forced_request(self) -> None:
        assert can_force_tool(object()) is True

    @pytest.mark.parametrize("mock", [MagicMock(), AsyncMock()])
    def test_a_mock_client_keeps_the_forced_request(self, mock: MagicMock) -> None:
        assert can_force_tool(mock) is True

    def test_a_client_that_says_no_gets_auto(self) -> None:
        assert can_force_tool(get_llm_client_for_key("claude_sonnet_5_5", budget_role="x")) is False
