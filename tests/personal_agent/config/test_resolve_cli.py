"""Unit tests for the config-resolve CLI (ADR-0099 D2.2, stage 3, FRE-651; ADR-0121, FRE-926).

AC-5 — resolving the entity_extraction role prints gpt-5.4-mini using only
committed files (config/model_roles.yaml + config/models.yaml), no running
container.

FRE-926 — resolve() now goes through resolve_role_target(), the same
Layer-3-bindings-backed resolver the client factory uses at runtime, so it
also answers for bindings-only roles (sub_agent, artifact_builder, vision)
that were removed from the legacy roles: matrix at FRE-920.

``--profile`` was removed in FRE-916 phase 2 (ADR-0121): it selected which
model-definition file to read, and there is now exactly one. Role assignment no
longer varies by deployment, so the question it answered cannot have two answers.
"""

from __future__ import annotations

import pytest

from personal_agent.config.model_loader import ModelRoleError, load_model_config
from personal_agent.config.resolve import main, resolve


class TestResolve:
    def test_resolve_primary_returns_qwen(self) -> None:
        assert resolve("primary") == "qwen3.6-35b-thinking"

    def test_resolve_entity_extraction_returns_gpt_5_4_mini(self) -> None:
        assert resolve("entity_extraction") == "gpt-5.4-mini"

    def test_resolve_sub_agent_returns_claude_sonnet(self) -> None:
        """FRE-926 AC-1 — sub_agent left the legacy matrix at FRE-920 (its value
        there had drifted); it must still resolve via its Layer-3 binding.
        """
        assert resolve("sub_agent") == "claude_sonnet"

    def test_resolve_artifact_builder_returns_claude_sonnet(self) -> None:
        """FRE-926 AC-1 — artifact_builder was never declared in the legacy matrix."""
        assert resolve("artifact_builder") == "claude_sonnet"

    def test_resolve_vision_returns_claude_sonnet(self) -> None:
        """FRE-926 AC-1 — vision was never declared in the legacy matrix."""
        assert resolve("vision") == "claude_sonnet"

    def test_resolves_every_bound_role_without_error(self) -> None:
        """Every role bound in config/model_roles.yaml's bindings: block must
        dereference — the CLI is the committed-files oracle, so a role it
        cannot answer for is a broken binding.
        """
        for role in load_model_config().roles:
            assert resolve(role)

    def test_unknown_role_raises(self) -> None:
        with pytest.raises(ModelRoleError):
            resolve("nonexistent_role")

    def test_raw_deployment_key_is_not_accepted_as_a_role(self) -> None:
        """FRE-926 AC-2 — resolve_role_target() falls back to treating an unbound
        name as a literal deployment key; the diagnostic must not let that leak
        through and answer for a deployment key that names no role.
        """
        with pytest.raises(ModelRoleError):
            resolve("claude_haiku")


class TestResolveCli:
    def test_cli_prints_resolved_key_to_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--role", "entity_extraction"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out == "gpt-5.4-mini\n"

    def test_cli_exits_nonzero_on_unknown_role(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--role", "nonexistent_role"])
        captured = capsys.readouterr()
        assert exit_code != 0
        assert "nonexistent_role" in captured.err

    def test_cli_rejects_the_retired_profile_flag(self) -> None:
        """A runbook still passing --profile must fail loudly, not silently
        resolve against the one catalog as though the flag had been honoured.
        """
        with pytest.raises(SystemExit):
            main(["--profile", "cloud", "--role", "entity_extraction"])
