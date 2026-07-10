"""Unit tests for AppConfig._validate_dev_test_profile_isolation (ADR-0112 AC-9, FRE-820).

Verifies that the model validator raises ValidationError whenever
substrate_profile is "dev" or "test" and a store resolves to a prod-fingerprint
URI — regardless of `environment` (the gap this closes: selecting the dev/test
PROFILE alone, without also setting APP_ENV=test, previously triggered no
isolation check at all) — and is silent otherwise.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from personal_agent.config.env_loader import Environment
from personal_agent.config.settings import AppConfig

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_TEST_SAFE_URLS: dict[str, object] = {
    "environment": Environment.DEVELOPMENT,  # deliberately NOT TEST — proves profile alone is load-bearing
    "neo4j_uri": "bolt://localhost:7688",  # non-prod port
    "elasticsearch_url": "http://localhost:9201",  # non-prod port
    "database_url": "postgresql+asyncpg://seshat_app:pw@localhost:5433/personal_agent_test",
    "database_admin_url": "postgresql+asyncpg://agent:pw@localhost:5433/personal_agent_test",
    "sysgraph_database_url": "postgresql+asyncpg://sysgraph_role:pw@localhost:5433/personal_agent_test",
}


def make_config(*, profile: str = "test", **overrides: object) -> AppConfig:
    """Build an AppConfig bypassing env-file loading.

    Starts from *_TEST_SAFE_URLS* (all fields on test-stack ports, environment
    deliberately non-TEST) plus the given *profile*, and applies *overrides*.

    Args:
        profile: The ``substrate_profile`` to set (default ``"test"``).
        **overrides: Field name -> value overrides.

    Returns:
        Validated AppConfig instance.
    """
    data: dict[str, object] = {**_TEST_SAFE_URLS, "substrate_profile": profile, **overrides}
    return AppConfig.model_validate(data)


# ---------------------------------------------------------------------------
# Guard fires — dev/test profile + prod URIs, independent of environment
# ---------------------------------------------------------------------------


class TestValidatorRaises:
    """Validator raises ValidationError for dev/test profile with prod-fingerprint URIs."""

    @pytest.mark.parametrize("profile", ["dev", "test"])
    def test_raises_when_profile_with_prod_neo4j_uri(self, profile: str) -> None:
        """ValidationError raised when dev/test profile + Neo4j on default port 7687."""
        with pytest.raises(ValidationError, match="prod/dev defaults"):
            make_config(
                profile=profile,
                neo4j_uri="bolt://localhost:7687",  # fre-375-allow: tests the prod-URI guard itself
            )

    def test_raises_when_test_profile_with_prod_elasticsearch_url(self) -> None:
        """ValidationError raised when test profile + Elasticsearch on default port 9200."""
        with pytest.raises(ValidationError, match="prod/dev defaults"):
            make_config(
                elasticsearch_url="http://localhost:9200",  # fre-375-allow: tests the prod-URI guard itself
            )

    def test_raises_when_test_profile_with_prod_postgres_url(self) -> None:
        """ValidationError raised when test profile + PostgreSQL on default port 5432."""
        with pytest.raises(ValidationError, match="prod/dev defaults"):
            make_config(
                database_url="postgresql+asyncpg://seshat_app:pw@localhost:5432/personal_agent"
            )

    def test_raises_when_test_profile_with_prod_admin_url(self) -> None:
        """ValidationError raised when test profile + admin URL on default port 5432."""
        with pytest.raises(ValidationError, match="prod/dev defaults"):
            make_config(
                database_admin_url="postgresql+asyncpg://agent:pw@localhost:5432/personal_agent"
            )

    def test_raises_when_test_profile_with_prod_sysgraph_url(self) -> None:
        """ValidationError raised when test profile + sysgraph URL on default port 5432."""
        with pytest.raises(ValidationError, match="prod/dev defaults"):
            make_config(
                sysgraph_database_url="postgresql+asyncpg://sysgraph_role:pw@localhost:5432/personal_agent"
            )

    def test_error_message_names_offending_uri(self) -> None:
        """Error message names the offending field for actionability."""
        with pytest.raises(ValidationError) as exc_info:
            make_config(
                neo4j_uri="bolt://localhost:7687",  # fre-375-allow: tests the prod-URI guard itself
            )
        assert "neo4j_uri" in str(exc_info.value)

    def test_profile_alone_is_load_bearing_without_test_environment(self) -> None:
        """The gap this closes: dev/test profile + DEVELOPMENT environment still raises.

        Prior to this validator, only ``environment == TEST`` triggered any
        isolation check — selecting the profile without also setting
        APP_ENV=test silently bypassed all enforcement.
        """
        with pytest.raises(ValidationError, match="prod/dev defaults"):
            make_config(
                profile="test",
                environment=Environment.DEVELOPMENT,
                neo4j_uri="bolt://localhost:7687",  # fre-375-allow: tests the prod-URI guard itself
            )


# ---------------------------------------------------------------------------
# Guard silent — bypass flag
# ---------------------------------------------------------------------------


class TestValidatorBypassFlag:
    """Validator is silent when allow_test_writes_to_prod_substrate=True."""

    def test_no_raise_when_bypass_flag_set_with_prod_neo4j(self) -> None:
        """No error when escape hatch is active, even with prod Neo4j URI."""
        cfg = make_config(
            neo4j_uri="bolt://localhost:7687",  # fre-375-allow: tests the prod-URI guard itself
            allow_test_writes_to_prod_substrate=True,
        )
        assert cfg.allow_test_writes_to_prod_substrate is True

    def test_no_raise_when_bypass_flag_set_with_all_prod_uris(self) -> None:
        """No error when bypass is active with all prod-fingerprint URIs."""
        cfg = make_config(
            neo4j_uri="bolt://localhost:7687",  # fre-375-allow: tests the prod-URI guard itself
            elasticsearch_url="http://localhost:9200",  # fre-375-allow: tests the prod-URI guard itself
            database_url="postgresql+asyncpg://agent:pw@localhost:5432/personal_agent",
            allow_test_writes_to_prod_substrate=True,
        )
        assert cfg.substrate_profile == "test"


# ---------------------------------------------------------------------------
# Guard silent — non-dev/test profiles
# ---------------------------------------------------------------------------


class TestValidatorSilentForNonDevTestProfile:
    """Validator does not fire for private/managed/managed_embedder profiles."""

    @pytest.mark.parametrize("profile", ["private", "managed", "managed_embedder"])
    def test_no_raise_for_non_dev_test_profile_with_prod_neo4j(self, profile: str) -> None:
        """No error for a non-dev/test profile regardless of URI (this validator's scope)."""
        cfg = make_config(
            profile=profile,
            neo4j_uri="bolt://localhost:7687",  # fre-375-allow: tests the prod-URI guard itself
        )
        assert cfg.substrate_profile == profile


# ---------------------------------------------------------------------------
# Guard silent — dev/test profile with non-prod URIs
# ---------------------------------------------------------------------------


class TestValidatorSilentForTestStackURIs:
    """Validator does not fire when dev/test profile uses non-prod-fingerprint URIs."""

    def test_no_raise_for_test_profile_with_test_stack_uris(self) -> None:
        """No error when all URIs use non-default ports."""
        cfg = make_config()  # _TEST_SAFE_URLS has non-prod ports for all fields
        assert cfg.substrate_profile == "test"

    def test_no_raise_for_dev_profile_with_test_stack_uris(self) -> None:
        """No error for the dev profile with non-prod-fingerprint URIs."""
        cfg = make_config(profile="dev")
        assert cfg.substrate_profile == "dev"

    def test_no_raise_for_test_profile_with_non_local_neo4j(self) -> None:
        """No error when Neo4j URI points to a non-localhost host."""
        cfg = make_config(neo4j_uri="bolt://neo4j-test:7687")
        assert cfg.substrate_profile == "test"


class TestValidatorDefaultTestSuiteConfigPasses:
    """A bare AppConfig() under the real test-suite environment never raises."""

    def test_bare_app_config_construction_does_not_raise(self) -> None:
        """conftest.py sets AGENT_SUBSTRATE_PROFILE=test + test-stack URIs, so this must not raise."""
        cfg = AppConfig()
        assert cfg.substrate_profile == "test"
