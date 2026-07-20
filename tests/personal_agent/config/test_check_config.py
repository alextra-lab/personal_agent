"""Unit tests for scripts/check_config.py (ADR-0099 D1/D4, FRE-649 + stage 2 FRE-650).

Each fixture under tests/personal_agent/config/fixtures/ isolates exactly one
violation; the real repo (post drift-correction) must pass every check clean.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from scripts.check_config import main

from personal_agent.config.config_guard import (
    _compose_deployment_profiles,
    check_dangling_model_references,
    check_deployment_manifest_matches_compose,
    check_embedding_fallback_identity,
    check_field_descriptions,
    check_matrix_shape,
    check_secret_field_plaintext_defaults,
    load_deployment_manifest,
    load_matrix,
    run_all_checks,
)
from personal_agent.config.settings import AppConfig

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_REPO_ROOT = Path(__file__).resolve().parents[3]


class TestRealRepoMatrixShape:
    """The real matrix is clean and carries the post-FRE-916 single-key shape.

    Replaces TestForbiddenRoleDivergence. The definition-drift fixture it used
    (`divergent_forbidden_role`) modelled the same key backed by different
    models in two catalogs — a state that became unrepresentable when phase 2
    collapsed to one catalog, so the fixture and its guard were both retired.
    """

    def test_passes_on_real_repo(self) -> None:
        findings = run_all_checks(_REPO_ROOT)
        assert findings == []

    def test_real_repo_roles_declare_a_bare_all_value(self) -> None:
        roles = load_matrix(_REPO_ROOT)["roles"]
        assert roles["entity_extraction"] == {"all": "gpt-5.4-mini"}
        assert roles["captains_log"] == {"all": "claude_sonnet"}
        assert roles["insights"] == {"all": "claude_sonnet"}
        # `primary`'s `divergence: allowed` row phase 2 collapsed (still present
        # here, unlike `sub_agent`'s: ADR-0121 T5 (FRE-920, master gate 2026-07-20)
        # removed that entry — a stale duplicate of the Layer-3 binding, never
        # actually consulted through this matrix in the first place).
        assert roles["primary"] == {"all": "qwen3.6-35b-thinking"}
        assert "sub_agent" not in roles

    def test_no_role_carries_a_retired_per_profile_key(self) -> None:
        for role, cfg in load_matrix(_REPO_ROOT)["roles"].items():
            assert set(cfg) == {"all"}, f"role {role!r} carries retired keys: {sorted(cfg)}"


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
        assert "config/models.yaml" in dangling[0].message

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
    """Every role declares exactly one `all:` key, and no retired keys (FRE-916 phase 2)."""

    def test_flags_missing_all_key_and_retired_per_profile_keys(self) -> None:
        matrix = load_matrix(_FIXTURES / "malformed_matrix_shape")
        findings = check_matrix_shape(matrix)
        messages = " ".join(f.message for f in findings)

        assert any("entity_extraction" in f.message and "no 'all'" in f.message for f in findings)
        # compressor HAS a valid `all`, so it must be flagged purely for carrying
        # the retired keys — which the loader now ignores silently.
        stale = [f for f in findings if "compressor" in f.message]
        assert len(stale) == 1
        assert "'cloud'" in stale[0].message
        assert "'divergence'" in stale[0].message
        assert "'local'" in stale[0].message
        assert all(f.severity == "policy" for f in findings)
        assert "entity_extraction" in messages and "compressor" in messages

    def test_no_false_positive_on_real_repo(self) -> None:
        matrix = load_matrix(_REPO_ROOT)
        findings = check_matrix_shape(matrix)
        assert findings == []


class TestDeploymentManifestMatchesCompose:
    """AC-5 — a profile's compose file must declare that same profile via
    AGENT_DEPLOYMENT_PROFILE (ADR-0099 D2.2, FRE-651; re-pointed off the retired
    AGENT_DEPLOYMENT_PROFILE in FRE-916 phase 2).
    """

    def test_fails_when_compose_declares_a_different_profile(self) -> None:
        root = _FIXTURES / "deployment_manifest_mismatch"
        manifest = load_deployment_manifest(root)
        findings = check_deployment_manifest_matches_compose(root, manifest)
        assert len(findings) == 1
        assert findings[0].check == "deployment_manifest_mismatch"
        assert findings[0].severity == "policy"
        assert "cloud" in findings[0].message
        assert "eval" in findings[0].message

    def test_no_false_positive_on_real_repo(self) -> None:
        manifest = load_deployment_manifest(_REPO_ROOT)
        findings = check_deployment_manifest_matches_compose(_REPO_ROOT, manifest)
        assert findings == []

    def test_run_all_checks_includes_deployment_checks_on_real_repo(self) -> None:
        assert run_all_checks(_REPO_ROOT) == []


class TestEmbeddingFallbackIdentity:
    """ADR-0112 AC-6 (FRE-821) — managed + local-fallback embedder configs must pin
    the same weights revision (exact match after an optional 'Qwen/'-style provider
    prefix is stripped from either side) — not a fuzzy/substring comparison.
    """

    def test_default_pairing_matches(self) -> None:
        assert check_embedding_fallback_identity(AppConfig()) == []

    def test_mismatched_revision_is_flagged(self) -> None:
        settings = AppConfig(
            managed_embedding_model="Qwen3-Embedding-8B",
            local_fallback_embedding_model="Qwen/Qwen3-Embedding-0.6B",
        )
        findings = check_embedding_fallback_identity(settings)
        assert len(findings) == 1
        assert findings[0].check == "embedding_fallback_identity_mismatch"
        assert findings[0].severity == "policy"
        assert "Qwen3-Embedding-8B" in findings[0].message
        assert "Qwen3-Embedding-0.6B" in findings[0].message

    def test_prefix_only_difference_is_not_flagged(self) -> None:
        settings = AppConfig(
            managed_embedding_model="Qwen3-Embedding-8B",
            local_fallback_embedding_model="Qwen/Qwen3-Embedding-8B",
        )
        assert check_embedding_fallback_identity(settings) == []

    def test_case_sensitive_mismatch_is_flagged(self) -> None:
        # Exact match, not fuzzy — a case difference is a real revision question,
        # not something to silently accept.
        settings = AppConfig(
            managed_embedding_model="qwen3-embedding-8b",
            local_fallback_embedding_model="Qwen3-Embedding-8B",
        )
        findings = check_embedding_fallback_identity(settings)
        assert len(findings) == 1

    def test_run_all_checks_includes_identity_check_on_real_repo(self) -> None:
        assert run_all_checks(_REPO_ROOT) == []


class TestComposeDeploymentProfileParsing:
    """Unit coverage for _compose_deployment_profiles's dict/list environment forms."""

    def test_dict_form_single_service(self) -> None:
        compose = {"services": {"gw": {"environment": {"AGENT_DEPLOYMENT_PROFILE": "cloud"}}}}
        assert _compose_deployment_profiles(compose) == {"cloud"}

    def test_list_form_single_service(self) -> None:
        compose = {"services": {"gw": {"environment": ["AGENT_DEPLOYMENT_PROFILE=cloud"]}}}
        assert _compose_deployment_profiles(compose) == {"cloud"}

    def test_two_services_agreeing_returns_one_value(self) -> None:
        compose = {
            "services": {
                "a": {"environment": {"AGENT_DEPLOYMENT_PROFILE": "cloud"}},
                "b": {"environment": {"AGENT_DEPLOYMENT_PROFILE": "cloud"}},
            }
        }
        assert _compose_deployment_profiles(compose) == {"cloud"}

    def test_two_services_disagreeing_returns_both_values(self) -> None:
        compose = {
            "services": {
                "a": {"environment": {"AGENT_DEPLOYMENT_PROFILE": "cloud"}},
                "b": {"environment": {"AGENT_DEPLOYMENT_PROFILE": "eval"}},
            }
        }
        assert _compose_deployment_profiles(compose) == {
            "cloud",
            "eval",
        }

    def test_no_environment_block_returns_empty_set(self) -> None:
        assert _compose_deployment_profiles({"services": {"gw": {"image": "x"}}}) == set()

    def test_service_with_no_matching_key_returns_empty_set(self) -> None:
        compose = {"services": {"gw": {"environment": {"OTHER_KEY": "value"}}}}
        assert _compose_deployment_profiles(compose) == set()


class TestFieldDescriptions:
    """ADR-0099 D4 — every AppConfig field must carry a non-empty description.

    A regression ratchet, not a cleanup: 311/311 real fields already comply.
    """

    def test_flags_field_with_no_description(self) -> None:
        class Model(BaseModel):
            documented: str = Field(default="x", description="Has a description")
            undocumented: str = Field(default="y")

        findings = check_field_descriptions(Model.model_fields)
        assert len(findings) == 1
        assert findings[0].check == "undocumented_field"
        assert findings[0].severity == "policy"
        assert "undocumented" in findings[0].message

    def test_flags_whitespace_only_description(self) -> None:
        class Model(BaseModel):
            blank: str = Field(default="y", description="   ")

        findings = check_field_descriptions(Model.model_fields)
        assert len(findings) == 1
        assert "blank" in findings[0].message

    def test_no_false_positive_on_real_repo(self) -> None:
        assert check_field_descriptions() == []


class TestSecretFieldPlaintextDefaults:
    """FRE-876 optional add-on — no secret-marked field defaults to a real plaintext value.

    check_committed_secrets (AC-8) only scans YAML/.env text for a *committed*
    secret value; it never looks at a secret field's own Python default.
    """

    def test_flags_secret_field_with_plaintext_default(self) -> None:
        class Model(BaseModel):
            api_key: str | None = Field(
                default="sk-real-looking-value", json_schema_extra={"secret": True}
            )

        findings = check_secret_field_plaintext_defaults(Model.model_fields)
        assert len(findings) == 1
        assert findings[0].check == "secret_field_plaintext_default"
        assert findings[0].severity == "policy"
        assert "api_key" in findings[0].message

    def test_does_not_flag_secret_field_defaulting_to_none(self) -> None:
        class Model(BaseModel):
            api_key: str | None = Field(default=None, json_schema_extra={"secret": True})

        assert check_secret_field_plaintext_defaults(Model.model_fields) == []

    def test_flags_secret_field_with_unexempted_default_factory(self) -> None:
        """A default_factory can return a hardcoded secret; field.default alone misses it."""

        class Model(BaseModel):
            api_key: str = Field(
                default_factory=lambda: "sk-hardcoded-in-a-factory",
                json_schema_extra={"secret": True},
            )

        findings = check_secret_field_plaintext_defaults(Model.model_fields)
        assert len(findings) == 1
        assert findings[0].check == "secret_field_plaintext_default"
        assert "api_key" in findings[0].message

    def test_exempted_default_factory_is_not_flagged(self) -> None:
        class Model(BaseModel):
            api_key: str = Field(
                default_factory=lambda: "dev-only-value",
                json_schema_extra={
                    "secret": True,
                    "secret_default_allow": "documented dev-only factory value",
                },
            )

        assert check_secret_field_plaintext_defaults(Model.model_fields) == []

    def test_exempted_secret_default_is_not_flagged(self) -> None:
        class Model(BaseModel):
            dev_password: str = Field(
                default="dev_only_password",
                json_schema_extra={
                    "secret": True,
                    "secret_default_allow": "documented local-dev-only convenience value",
                },
            )

        assert check_secret_field_plaintext_defaults(Model.model_fields) == []

    def test_empty_string_exemption_reason_does_not_count(self) -> None:
        class Model(BaseModel):
            dev_password: str = Field(
                default="dev_only_password",
                json_schema_extra={"secret": True, "secret_default_allow": "   "},
            )

        findings = check_secret_field_plaintext_defaults(Model.model_fields)
        assert len(findings) == 1
        assert findings[0].check == "secret_field_plaintext_default"

    def test_flags_exemption_key_on_non_secret_field_as_misuse(self) -> None:
        class Model(BaseModel):
            plain: str = Field(default="x", json_schema_extra={"secret_default_allow": "bogus"})

        findings = check_secret_field_plaintext_defaults(Model.model_fields)
        assert len(findings) == 1
        assert findings[0].check == "secret_default_allow_without_secret_marker"
        assert findings[0].severity == "policy"
        assert "plain" in findings[0].message

    def test_no_false_positive_on_real_repo(self) -> None:
        """Proves the neo4j_password exemption works end to end on the real repo."""
        assert check_secret_field_plaintext_defaults() == []

    def test_neo4j_password_carries_the_exemption(self) -> None:
        field = AppConfig.model_fields["neo4j_password"]
        extra = field.json_schema_extra
        assert isinstance(extra, dict)
        assert isinstance(extra.get("secret_default_allow"), str)
        assert extra["secret_default_allow"].strip()
