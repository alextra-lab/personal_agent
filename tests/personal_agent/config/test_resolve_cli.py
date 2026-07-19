"""Unit tests for the config-resolve CLI (ADR-0099 D2.2, stage 3, FRE-651).

AC-5 — resolving the entity_extraction role prints gpt-5.4-mini using only
committed files (config/model_roles.yaml + config/models.yaml), no running
container.

``--profile`` was removed in FRE-916 phase 2 (ADR-0121): it selected which
model-definition file to read, and there is now exactly one. Role assignment no
longer varies by deployment, so the question it answered cannot have two answers.
"""

from __future__ import annotations

import pytest

from personal_agent.config.model_loader import ModelRoleError
from personal_agent.config.resolve import main, resolve


class TestResolve:
    def test_resolve_entity_extraction_returns_gpt_5_4_mini(self) -> None:
        assert resolve("entity_extraction") == "gpt-5.4-mini"

    def test_resolves_every_declared_role_without_error(self) -> None:
        """The matrix's roles must all dereference — the CLI is the committed-files
        oracle, so a role it cannot answer for is a broken matrix.
        """
        from personal_agent.config.config_guard import load_matrix, repo_root

        for role in load_matrix(repo_root())["roles"]:
            assert resolve(role)

    def test_unknown_role_raises(self) -> None:
        with pytest.raises(ModelRoleError):
            resolve("nonexistent_role")


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
