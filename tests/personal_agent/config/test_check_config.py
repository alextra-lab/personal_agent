"""Unit tests for scripts/check_config.py (ADR-0099 D1/D4, FRE-649 + stage 2 FRE-650).

Each fixture under tests/personal_agent/config/fixtures/ isolates exactly one
violation; the real repo (post drift-correction) must pass every check clean.
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_config import main

from personal_agent.config.config_guard import check_matrix_shape, load_matrix, run_all_checks

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_REPO_ROOT = Path(__file__).resolve().parents[3]


class TestForbiddenRoleDivergence:
    """AC-3 — guard fails on a definition-drift forbidden role; passes on the real repo."""

    def test_fails_on_divergent_forbidden_role_fixture(self) -> None:
        findings = run_all_checks(_FIXTURES / "divergent_forbidden_role")
        names = [f.check for f in findings]
        assert "forbidden_role_divergence" in names
        messages = " ".join(f.message for f in findings)
        assert "entity_extraction" in messages

    def test_cli_exits_nonzero_on_divergent_forbidden_role_fixture(self) -> None:
        exit_code = main(["--root", str(_FIXTURES / "divergent_forbidden_role")])
        assert exit_code != 0

    def test_passes_on_real_repo(self) -> None:
        findings = run_all_checks(_REPO_ROOT)
        assert findings == []

    def test_real_repo_forbidden_roles_declare_all_value(self) -> None:
        matrix = load_matrix(_REPO_ROOT)
        roles = matrix["roles"]
        assert roles["entity_extraction"] == {"divergence": "forbidden", "all": "gpt-5.4-mini"}
        assert roles["captains_log"] == {"divergence": "forbidden", "all": "claude_sonnet"}
        assert roles["insights"] == {"divergence": "forbidden", "all": "claude_sonnet"}


class TestOrphanEnvKeys:
    """AC-4 — guard flags exactly a planted orphan AGENT_* key."""

    def test_flags_planted_orphan_env_key(self) -> None:
        findings = run_all_checks(_FIXTURES / "orphan_env")
        orphan_findings = [f for f in findings if f.check == "orphan_env_key"]
        assert len(orphan_findings) == 1
        assert "AGENT_TOTALLY_MADE_UP_KEY" in orphan_findings[0].message

    def test_no_false_positive_on_real_env_example(self) -> None:
        findings = run_all_checks(_REPO_ROOT)
        orphan_findings = [f for f in findings if f.check == "orphan_env_key"]
        assert orphan_findings == []


class TestCommittedSecrets:
    """AC-8 — guard fails on a committed secret value; passes on the real repo."""

    def test_fails_on_committed_secret_fixture(self) -> None:
        findings = run_all_checks(_FIXTURES / "committed_secret")
        secret_findings = [f for f in findings if f.check == "committed_secret"]
        assert len(secret_findings) == 1
        assert "anthropic_api_key" not in secret_findings[0].message
        assert "openai_api_key" in secret_findings[0].message

    def test_no_false_positive_on_real_repo(self) -> None:
        findings = run_all_checks(_REPO_ROOT)
        secret_findings = [f for f in findings if f.check == "committed_secret"]
        assert secret_findings == []


class TestDanglingModelReference:
    """AC-9 — guard fails on a matrix role resolving to an undefined model."""

    def test_fails_on_dangling_model_reference(self) -> None:
        findings = run_all_checks(_FIXTURES / "dangling_reference")
        dangling = [f for f in findings if f.check == "dangling_model_reference"]
        assert len(dangling) == 1
        assert "gpt-9-ghost" in dangling[0].message
        assert "local" in dangling[0].message

    def test_no_false_positive_on_real_repo(self) -> None:
        findings = run_all_checks(_REPO_ROOT)
        dangling = [f for f in findings if f.check == "dangling_model_reference"]
        assert dangling == []


class TestMatrixShape:
    """A role's declared keys must match its own divergence value (FRE-650)."""

    def test_flags_forbidden_role_missing_all_and_allowed_role_missing_local_or_cloud(
        self,
    ) -> None:
        matrix = load_matrix(_FIXTURES / "malformed_matrix_shape")
        findings = check_matrix_shape(matrix)
        messages = " ".join(f.message for f in findings)

        assert any("entity_extraction" in f.message and "no 'all'" in f.message for f in findings)
        assert any(
            "entity_extraction" in f.message and "declares 'local'/'cloud'" in f.message
            for f in findings
        )
        assert any("compressor" in f.message and "declares neither" in f.message for f in findings)
        assert any("compressor" in f.message and "declares 'all'" in f.message for f in findings)
        assert all(f.severity == "policy" for f in findings)
        assert "entity_extraction" in messages and "compressor" in messages

    def test_no_false_positive_on_real_repo(self) -> None:
        matrix = load_matrix(_REPO_ROOT)
        findings = check_matrix_shape(matrix)
        assert findings == []
