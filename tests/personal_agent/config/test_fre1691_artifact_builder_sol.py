"""FRE-1691 — ``artifact_builder`` binds ``gpt-6.1-sol`` (AC-1).

The owner decided the swap after the FRE-1668 pilot: Sol built all three rich pages, and
Sonnet 5 and Sonnet 5.5 each built one of three before the 32,768-token output ceiling.
The role stays ``open: true`` so the owner can still pick another builder per build.
Offline: reads the committed config only.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from personal_agent.config import load_model_config
from personal_agent.config.model_loader import resolve_role_model_key
from personal_agent.llm_client.factory import get_llm_client
from personal_agent.llm_client.litellm_client import LiteLLMClient


def _bindings() -> dict[str, dict[str, object]]:
    return yaml.safe_load(Path("config/model_roles.yaml").read_text())["bindings"]


def test_artifact_builder_binding_is_sol_and_stays_open() -> None:
    """AC-1: the committed binding names Sol and keeps the per-build pick open."""
    assert _bindings()["artifact_builder"] == {"deployment": "gpt-6.1-sol", "open": True}


def test_default_artifact_builder_resolution_is_sol() -> None:
    """AC-1: the default resolution, and the client built for the role, use Sol."""
    assert resolve_role_model_key("artifact_builder") == "gpt-6.1-sol"

    client = get_llm_client(role_name="artifact_builder")

    assert isinstance(client, LiteLLMClient)
    assert client.budget_role == "artifact_builder"
    assert client.model_id == load_model_config().models["gpt-6.1-sol"].id


def test_the_other_bindings_keep_their_deployments() -> None:
    """The swap leaves session_summary and vision on Sonnet 5."""
    assert resolve_role_model_key("session_summary") == "claude_sonnet"
    assert resolve_role_model_key("vision") == "claude_sonnet"
