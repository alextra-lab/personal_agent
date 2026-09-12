"""ADR-0150 D3 — the thoroughness round budget (FRE-1493 AC-2 load half, AC-4 guard)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from personal_agent.config import AppConfig
from personal_agent.config.settings import THOROUGHNESS_LEVELS

_REPO_ROOT = Path(__file__).resolve().parents[2]


class TestRoundsFollowTheCap:
    def test_every_level_equals_the_cap_by_default(self) -> None:
        """AC-4: no budget changes on merge — each level reads the cap."""
        config = AppConfig(sub_agent_max_tool_iterations=5)

        assert config.sub_agent_rounds_by_thoroughness == {}
        assert {lvl: config.sub_agent_rounds_for(lvl) for lvl in THOROUGHNESS_LEVELS} == {
            "quick": 5,
            "standard": 5,
            "thorough": 5,
        }

    def test_levels_follow_a_raised_cap_too(self) -> None:
        """The FRE-1487 study raises the cap. An unset level follows it both ways."""
        config = AppConfig(sub_agent_max_tool_iterations=20)

        assert all(config.sub_agent_rounds_for(lvl) == 20 for lvl in THOROUGHNESS_LEVELS)

    def test_an_explicit_level_is_read(self) -> None:
        config = AppConfig(
            sub_agent_max_tool_iterations=5,
            sub_agent_rounds_by_thoroughness={"quick": 2, "standard": 4, "thorough": 5},
        )

        assert config.sub_agent_rounds_for("quick") == 2
        assert config.sub_agent_rounds_for("standard") == 4
        assert config.sub_agent_rounds_for("thorough") == 5


class TestOverCapIsRefused:
    def test_over_cap_value_is_refused_at_load(self) -> None:
        """AC-2: a value above sub_agent_max_tool_iterations does not load."""
        with pytest.raises(ValidationError, match="sub_agent_rounds_by_thoroughness"):
            AppConfig(
                sub_agent_max_tool_iterations=5,
                sub_agent_rounds_by_thoroughness={"thorough": 6},
            )

    def test_negative_value_is_refused_at_load(self) -> None:
        with pytest.raises(ValidationError, match="sub_agent_rounds_by_thoroughness"):
            AppConfig(sub_agent_rounds_by_thoroughness={"quick": -1})

    def test_unknown_level_is_refused_at_load(self) -> None:
        with pytest.raises(ValidationError):
            AppConfig(sub_agent_rounds_by_thoroughness={"exhaustive": 3})

    def test_env_form_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_SUB_AGENT_ROUNDS_BY_THOROUGHNESS", '{"quick": 2}')

        assert AppConfig(sub_agent_max_tool_iterations=5).sub_agent_rounds_for("quick") == 2


class TestGuardedLimitsUnchanged:
    """AC-4 guard (FRE-1496): retired limits are committed; every level equals the cap."""

    def test_guarded_limit_defaults_are_committed(self) -> None:
        """FRE-1496 AC-1/AC-2: the three retired study values are now baselines."""
        fields = AppConfig.model_fields
        assert fields["sub_agent_max_tool_iterations"].default == 20
        assert fields["orchestrator_task_timeout_seconds"].default == 900

        # Verify every level equals the cap at the committed baseline
        config = AppConfig()
        assert all(config.sub_agent_rounds_for(lvl) == 20 for lvl in THOROUGHNESS_LEVELS)

    def test_sub_agent_role_defaults_are_committed(self) -> None:
        """FRE-1496 AC-1: config/model_roles.yaml has the committed values."""
        roles = yaml.safe_load((_REPO_ROOT / "config" / "model_roles.yaml").read_text())
        assert roles["bindings"]["sub_agent"]["default_timeout"] == 600

        # Verify levels follow a patched cap too
        config = AppConfig(sub_agent_max_tool_iterations=15)
        assert all(config.sub_agent_rounds_for(lvl) == 15 for lvl in THOROUGHNESS_LEVELS)
