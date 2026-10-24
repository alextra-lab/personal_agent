"""FRE-1646 — ``captains_log`` binds ``gpt-6.1-sol`` (AC-1).

The owner chose the binding on the FRE-1640 study (F4): ``gpt-6.1-sol`` was not worse than
the ``claude_sonnet`` control on 20 of 20 blind pairs (bar 16). ``session_summary`` shares the
``captains_log`` budget lane but keeps its own binding. Offline: reads the committed config only.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml

from personal_agent.config import load_model_config
from personal_agent.config.model_loader import resolve_role_model_key
from personal_agent.llm_client.dspy_adapter import configure_dspy_lm


def _bindings() -> dict[str, dict[str, object]]:
    return yaml.safe_load(Path("config/model_roles.yaml").read_text())["bindings"]


def test_captains_log_resolves_to_sol_and_session_summary_stays_on_sonnet() -> None:
    """AC-1: the reflection role moves, the summariser role does not."""
    assert resolve_role_model_key("captains_log") == "gpt-6.1-sol"
    assert load_model_config().models["gpt-6.1-sol"].id == "gpt-6.1-sol"
    assert resolve_role_model_key("session_summary") == "claude_sonnet"


def test_the_binding_is_declared_in_model_roles_yaml() -> None:
    """The committed binding names the deployment (no inherit, no override)."""
    bindings = _bindings()

    assert bindings["captains_log"] == {"deployment": "gpt-6.1-sol"}
    assert bindings["session_summary"] == {"deployment": "claude_sonnet"}


def test_only_captains_log_and_artifact_builder_bind_sol() -> None:
    """Sol binds the two roles the owner chose (FRE-1646, FRE-1691), and no other role."""
    resolved = {role: resolve_role_model_key(role) for role in _bindings()}

    assert {role for role, key in resolved.items() if key == "gpt-6.1-sol"} == {
        "captains_log",
        "artifact_builder",
    }


def test_reflection_dspy_path_sends_no_temperature_to_sol() -> None:
    """The DSPy path carries the declared reasoning effort and no sampler (ticket scope 4).

    Sol cannot turn reasoning off and rejects a sampler, so a ``temperature`` here would
    be a provider error on every reflection.
    """
    with patch("personal_agent.llm_client.dspy_adapter.settings") as settings:
        settings.openai_api_key = "test-key"
        settings.llm_timeout_seconds = 60
        lm = configure_dspy_lm("captains_log")

    assert lm.model == "openai/gpt-6.1-sol"
    assert lm.kwargs["temperature"] is None
    assert lm.kwargs["reasoning_effort"] == "medium"
    assert "top_p" not in lm.kwargs
