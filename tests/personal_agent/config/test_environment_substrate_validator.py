"""Unit tests for AppConfig._validate_substrate_isolation (FRE-375).

Verifies that the model validator raises ValidationError when the TEST
environment is combined with prod-fingerprint substrate URIs, and passes in
all other scenarios.
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
    "environment": Environment.TEST,
    "neo4j_uri": "bolt://localhost:7688",  # non-prod port
    "elasticsearch_url": "http://localhost:9201",  # non-prod port
    "database_url": "postgresql+asyncpg://agent:pw@localhost:5433/personal_agent_test",
}


def make_config(**overrides: object) -> AppConfig:
    """Build an AppConfig bypassing env-file loading.

    Starts from *_TEST_SAFE_URLS* (all three URIs on test-stack ports) and
    applies *overrides* on top.  Uses ``model_validate`` so the dict values
    take precedence over any inherited env vars.

    Args:
        **overrides: Field name → value overrides.

    Returns:
        Validated AppConfig instance.
    """
    data: dict[str, object] = {**_TEST_SAFE_URLS, **overrides}
    return AppConfig.model_validate(data)


# ---------------------------------------------------------------------------
# Guard fires — TEST + prod URIs
# ---------------------------------------------------------------------------


class TestValidatorRaises:
    """Validator raises ValidationError for TEST env with prod-fingerprint URIs."""

    def test_raises_when_test_env_with_prod_neo4j_uri(self) -> None:
        """ValidationError raised when TEST env + Neo4j on default port 7687."""
        with pytest.raises(ValidationError, match="prod/dev defaults"):
            make_config(neo4j_uri="bolt://localhost:7687")

    def test_raises_when_test_env_with_prod_elasticsearch_url(self) -> None:
        """ValidationError raised when TEST env + Elasticsearch on default port 9200."""
        with pytest.raises(ValidationError, match="prod/dev defaults"):
            make_config(elasticsearch_url="http://localhost:9200")

    def test_raises_when_test_env_with_prod_postgres_url(self) -> None:
        """ValidationError raised when TEST env + PostgreSQL on default port 5432."""
        with pytest.raises(ValidationError, match="prod/dev defaults"):
            make_config(
                database_url="postgresql+asyncpg://agent:pw@localhost:5432/personal_agent"
            )

    def test_raises_when_test_env_with_multiple_prod_uris(self) -> None:
        """ValidationError raised when TEST env + multiple prod-fingerprint URIs."""
        with pytest.raises(ValidationError, match="prod/dev defaults"):
            make_config(
                neo4j_uri="bolt://localhost:7687",
                elasticsearch_url="http://localhost:9200",
            )

    def test_error_message_names_offending_uri(self) -> None:
        """Error message names the offending URI for actionability."""
        with pytest.raises(ValidationError) as exc_info:
            make_config(neo4j_uri="bolt://localhost:7687")
        # The error message must reference the offending field
        assert "neo4j_uri" in str(exc_info.value)

    def test_raises_with_127_0_0_1_neo4j(self) -> None:
        """Loopback alias 127.0.0.1 is treated the same as localhost."""
        with pytest.raises(ValidationError, match="prod/dev defaults"):
            make_config(neo4j_uri="bolt://127.0.0.1:7687")


# ---------------------------------------------------------------------------
# Guard silent — bypass flag
# ---------------------------------------------------------------------------


class TestValidatorBypassFlag:
    """Validator is silent when allow_test_writes_to_prod_substrate=True."""

    def test_no_raise_when_bypass_flag_set_with_prod_neo4j(self) -> None:
        """No error when escape hatch is active, even with prod Neo4j URI."""
        cfg = make_config(
            neo4j_uri="bolt://localhost:7687",
            allow_test_writes_to_prod_substrate=True,
        )
        assert cfg.allow_test_writes_to_prod_substrate is True

    def test_no_raise_when_bypass_flag_set_with_all_prod_uris(self) -> None:
        """No error when bypass is active with all three prod-fingerprint URIs."""
        cfg = make_config(
            neo4j_uri="bolt://localhost:7687",
            elasticsearch_url="http://localhost:9200",
            database_url="postgresql+asyncpg://agent:pw@localhost:5432/personal_agent",
            allow_test_writes_to_prod_substrate=True,
        )
        assert cfg.environment == Environment.TEST


# ---------------------------------------------------------------------------
# Guard silent — non-TEST environments
# ---------------------------------------------------------------------------


class TestValidatorSilentForNonTestEnv:
    """Validator does not fire for DEVELOPMENT or PRODUCTION environments."""

    def test_no_raise_for_development_env_with_prod_neo4j(self) -> None:
        """No error for DEVELOPMENT environment regardless of URI."""
        cfg = make_config(
            environment=Environment.DEVELOPMENT,
            neo4j_uri="bolt://localhost:7687",
        )
        assert cfg.environment == Environment.DEVELOPMENT

    def test_no_raise_for_production_env_with_prod_neo4j(self) -> None:
        """No error for PRODUCTION environment regardless of URI."""
        cfg = make_config(
            environment=Environment.PRODUCTION,
            neo4j_uri="bolt://localhost:7687",
        )
        assert cfg.environment == Environment.PRODUCTION

    def test_no_raise_for_staging_env_with_prod_neo4j(self) -> None:
        """No error for STAGING environment regardless of URI."""
        cfg = make_config(
            environment=Environment.STAGING,
            neo4j_uri="bolt://localhost:7687",
        )
        assert cfg.environment == Environment.STAGING


# ---------------------------------------------------------------------------
# Guard silent — test env with non-prod URIs
# ---------------------------------------------------------------------------


class TestValidatorSilentForTestStackURIs:
    """Validator does not fire when TEST env uses non-prod-fingerprint URIs."""

    def test_no_raise_for_test_env_with_test_stack_uris(self) -> None:
        """No error when all three URIs use non-default ports."""
        cfg = make_config()  # _TEST_SAFE_URLS has non-prod ports for all three
        assert cfg.environment == Environment.TEST

    def test_no_raise_for_test_env_with_non_local_neo4j(self) -> None:
        """No error when Neo4j URI points to a non-localhost host."""
        cfg = make_config(neo4j_uri="bolt://neo4j-test:7687")
        assert cfg.environment == Environment.TEST

    def test_no_raise_for_test_env_with_non_local_elasticsearch(self) -> None:
        """No error when Elasticsearch URL points to a non-localhost host."""
        cfg = make_config(elasticsearch_url="http://es-test:9200")
        assert cfg.environment == Environment.TEST
