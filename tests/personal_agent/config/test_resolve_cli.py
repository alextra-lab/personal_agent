"""Unit tests for the config-resolve CLI (ADR-0099 D2.2, stage 3, FRE-651).

AC-5 — resolving the cloud profile's entity_extraction role prints gpt-5.4-mini
using only committed files (config/deployment.yaml + config/model_roles.yaml +
the model-definition files), no running container.
"""

from __future__ import annotations

import pytest
from personal_agent.config.resolve import main, resolve

from personal_agent.config.config_guard import DeploymentProfileError
from personal_agent.config.model_loader import ModelRoleError


class TestResolve:
    def test_resolve_cloud_entity_extraction_returns_gpt_5_4_mini(self) -> None:
        assert resolve("cloud", "entity_extraction") == "gpt-5.4-mini"

    def test_eval_profile_resolves_same_as_cloud(self) -> None:
        assert resolve("eval", "entity_extraction") == resolve("cloud", "entity_extraction")

    def test_unknown_profile_raises(self) -> None:
        with pytest.raises(DeploymentProfileError):
            resolve("nonexistent", "entity_extraction")

    def test_unknown_role_raises(self) -> None:
        with pytest.raises(ModelRoleError):
            resolve("cloud", "nonexistent_role")


class TestResolveCli:
    def test_cli_prints_resolved_key_to_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--profile", "cloud", "--role", "entity_extraction"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out == "gpt-5.4-mini\n"

    def test_cli_exits_nonzero_on_unknown_profile(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--profile", "nonexistent", "--role", "entity_extraction"])
        captured = capsys.readouterr()
        assert exit_code != 0
        assert "nonexistent" in captured.err

    def test_cli_exits_nonzero_on_unknown_role(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--profile", "cloud", "--role", "nonexistent_role"])
        captured = capsys.readouterr()
        assert exit_code != 0
        assert "nonexistent_role" in captured.err
