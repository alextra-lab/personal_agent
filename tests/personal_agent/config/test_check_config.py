"""Unit tests for scripts/check_config.py (ADR-0099 D1/D4, FRE-649 + stage 2 FRE-650).

Each fixture under tests/personal_agent/config/fixtures/ isolates exactly one
violation; the real repo (post drift-correction) must pass every check clean.
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_config import main

from personal_agent.config.config_guard import (
    _compose_model_config_paths,
    _normalize_container_model_config_path,
    check_deployment_manifest_internal_consistency,
    check_deployment_manifest_matches_compose,
    check_matrix_shape,
    load_deployment_manifest,
    load_matrix,
    run_all_checks,
)

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


class TestNoRoleHeaders:
    """AC-2(a) — no config/*.yaml re-declares a role-assignment header (ADR-0099 D1 stage 4, FRE-652).

    Role assignment has lived ONLY in config/model_roles.yaml since stage 2 (FRE-650); a
    reintroduced `<role>_role:` header would silently reopen the assignment-drift surface
    stage 2 closed, even though the loader already ignores it. This makes the ADR's manual
    "grep returns zero" seam check (ADR-0099 §Verification, assembled-seam item 2) a
    permanent CI/pre-commit gate.
    """

    def test_fails_on_reintroduced_role_header_fixture(self) -> None:
        findings = run_all_checks(_FIXTURES / "role_header_reintroduced")
        header_findings = [f for f in findings if f.check == "role_header_reintroduced"]
        assert len(header_findings) == 1
        assert header_findings[0].severity == "policy"
        assert "entity_extraction" in header_findings[0].message

    def test_no_false_positive_on_real_repo(self) -> None:
        findings = run_all_checks(_REPO_ROOT)
        header_findings = [f for f in findings if f.check == "role_header_reintroduced"]
        assert header_findings == []


class TestRetiredModelDefinitionYamlsStayGone:
    """ADR-0099 stage 4 (FRE-652) — the redundant model-definition YAMLs are retired.

    config/models.eval.yaml was already retired (FRE-735); this is the stage-4 half:
    models-baseline.yaml and models.medium.yaml consolidated away, leaving only the two
    files config/model_roles.yaml's active_profiles names (models.yaml, models.cloud.yaml).
    """

    def test_models_baseline_yaml_does_not_exist(self) -> None:
        assert not (_REPO_ROOT / "config" / "models-baseline.yaml").exists()

    def test_models_medium_yaml_does_not_exist(self) -> None:
        assert not (_REPO_ROOT / "config" / "models.medium.yaml").exists()


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


class TestDeploymentManifestInternalConsistency:
    """A profile row's model_config_path must name the same file as its own
    env_overrides.AGENT_MODEL_CONFIG_PATH (ADR-0099 D2.2, FRE-651 — codex
    plan-review finding: without this, config-resolve could silently answer
    from a different file than the one env_overrides documents as deployed).
    """

    def test_fails_on_internal_mismatch_fixture(self) -> None:
        manifest = load_deployment_manifest(_FIXTURES / "deployment_manifest_internal_mismatch")
        findings = check_deployment_manifest_internal_consistency(manifest)
        assert len(findings) == 1
        assert findings[0].check == "deployment_manifest_internal_mismatch"
        assert findings[0].severity == "policy"
        assert "config/models.yaml" in findings[0].message
        assert "config/models.cloud.yaml" in findings[0].message

    def test_no_false_positive_on_real_repo(self) -> None:
        manifest = load_deployment_manifest(_REPO_ROOT)
        findings = check_deployment_manifest_internal_consistency(manifest)
        assert findings == []


class TestDeploymentManifestMatchesCompose:
    """AC-5 — the manifest's declared AGENT_MODEL_CONFIG_PATH must match what
    the profile's actual compose file sets (ADR-0099 D2.2, FRE-651).
    """

    def test_fails_on_manifest_compose_mismatch_fixture(self) -> None:
        root = _FIXTURES / "deployment_manifest_mismatch"
        manifest = load_deployment_manifest(root)
        findings = check_deployment_manifest_matches_compose(root, manifest)
        assert len(findings) == 1
        assert findings[0].check == "deployment_manifest_mismatch"
        assert findings[0].severity == "policy"
        assert "models.WRONG.yaml" in findings[0].message
        assert "models.cloud.yaml" in findings[0].message

    def test_no_false_positive_on_real_repo(self) -> None:
        manifest = load_deployment_manifest(_REPO_ROOT)
        findings = check_deployment_manifest_matches_compose(_REPO_ROOT, manifest)
        assert findings == []

    def test_run_all_checks_includes_deployment_checks_on_real_repo(self) -> None:
        assert run_all_checks(_REPO_ROOT) == []


class TestComposeModelConfigPathParsing:
    """Unit coverage for _compose_model_config_paths's dict/list environment forms."""

    def test_dict_form_single_service(self) -> None:
        compose = {
            "services": {"gw": {"environment": {"AGENT_MODEL_CONFIG_PATH": "/app/config/x.yaml"}}}
        }
        assert _compose_model_config_paths(compose) == {"/app/config/x.yaml"}

    def test_list_form_single_service(self) -> None:
        compose = {
            "services": {"gw": {"environment": ["AGENT_MODEL_CONFIG_PATH=/app/config/x.yaml"]}}
        }
        assert _compose_model_config_paths(compose) == {"/app/config/x.yaml"}

    def test_two_services_agreeing_returns_one_value(self) -> None:
        compose = {
            "services": {
                "a": {"environment": {"AGENT_MODEL_CONFIG_PATH": "/app/config/x.yaml"}},
                "b": {"environment": {"AGENT_MODEL_CONFIG_PATH": "/app/config/x.yaml"}},
            }
        }
        assert _compose_model_config_paths(compose) == {"/app/config/x.yaml"}

    def test_two_services_disagreeing_returns_both_values(self) -> None:
        compose = {
            "services": {
                "a": {"environment": {"AGENT_MODEL_CONFIG_PATH": "/app/config/x.yaml"}},
                "b": {"environment": {"AGENT_MODEL_CONFIG_PATH": "/app/config/y.yaml"}},
            }
        }
        assert _compose_model_config_paths(compose) == {
            "/app/config/x.yaml",
            "/app/config/y.yaml",
        }

    def test_no_environment_block_returns_empty_set(self) -> None:
        assert _compose_model_config_paths({"services": {"gw": {"image": "x"}}}) == set()

    def test_service_with_no_matching_key_returns_empty_set(self) -> None:
        compose = {"services": {"gw": {"environment": {"OTHER_KEY": "value"}}}}
        assert _compose_model_config_paths(compose) == set()


class TestNormalizeContainerModelConfigPath:
    """Unit coverage for the /app/ container-mount-prefix normalization."""

    def test_strips_app_prefix(self) -> None:
        assert _normalize_container_model_config_path("/app/config/models.cloud.yaml") == (
            "config/models.cloud.yaml"
        )

    def test_passes_through_relative_path_unchanged(self) -> None:
        assert _normalize_container_model_config_path("config/models.yaml") == "config/models.yaml"
