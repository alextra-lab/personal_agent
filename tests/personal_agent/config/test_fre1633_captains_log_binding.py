"""FRE-1633 — ``captains_log`` binds ``claude_haiku_5_5`` (AC-4).

The binding follows the swapped-order side-by-side (scripts/eval/fre1628_captains_log_ab):
Haiku 5.5 was not worse on 9 of 10 pairs against ``claude_sonnet`` (bar 8), and
``gpt-6-luna`` on 5 of 10. Offline: reads the committed config only.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from personal_agent.config import load_model_config
from personal_agent.config.model_loader import resolve_role_model_key


def test_captains_log_resolves_to_haiku_5_5() -> None:
    """The role resolves to the Haiku 5.5 deployment, not to Sonnet."""
    key = resolve_role_model_key("captains_log")

    assert key == "claude_haiku_5_5"
    assert load_model_config().models[key].id == "claude-haiku-5-5"


def test_the_binding_is_declared_in_model_roles_yaml() -> None:
    """The committed binding names the deployment (no inherit, no override)."""
    bindings = yaml.safe_load(Path("config/model_roles.yaml").read_text())["bindings"]

    assert bindings["captains_log"] == {"deployment": "claude_haiku_5_5"}


def test_only_captains_log_moved_and_luna_binds_nothing() -> None:
    """Haiku 5.5 binds the one role it passed, and the failed candidate binds none."""
    bindings = yaml.safe_load(Path("config/model_roles.yaml").read_text())["bindings"]
    resolved = {role: resolve_role_model_key(role) for role in bindings}

    assert {r for r, k in resolved.items() if k == "claude_haiku_5_5"} == {"captains_log"}
    assert "gpt-6-luna" not in resolved.values()
