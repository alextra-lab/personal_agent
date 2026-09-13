"""Unit tests for AppConfig._validate_eval_deployment_isolation (FRE-1372 reopen).

Verifies that the model validator raises ValidationError whenever
deployment_profile is "eval" and a store does not resolve to an `-eval` host —
independent of substrate_profile — and is silent otherwise.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from personal_agent.config.settings import AppConfig

_EVAL_SAFE_URLS: dict[str, object] = {
    "deployment_profile": "eval",
    "neo4j_uri": "bolt://neo4j-eval:7687",
    "elasticsearch_url": "http://elasticsearch-eval:9200",
    "database_url": "postgresql+asyncpg://seshat_app:pw@postgres-eval:5432/personal_agent",
    "database_admin_url": "postgresql+asyncpg://agent:pw@postgres-eval:5432/personal_agent",
}


def make_config(**overrides: object) -> AppConfig:
    """Build an AppConfig bypassing env-file loading.

    Starts from *_EVAL_SAFE_URLS* (deployment_profile="eval", all four fields on
    `-eval` hosts) plus *overrides*.

    Args:
        **overrides: Field name -> value overrides.

    Returns:
        Validated AppConfig instance.
    """
    data: dict[str, object] = {**_EVAL_SAFE_URLS, **overrides}
    return AppConfig.model_validate(data)


class TestValidatorRaises:
    """Validator raises ValidationError for eval profile with non-`-eval` hosts."""

    def test_raises_when_eval_profile_with_prod_neo4j_uri(self) -> None:
        """ValidationError raised when eval profile + Neo4j on production's hostname."""
        with pytest.raises(ValidationError, match="FRE-1372"):
            make_config(
                neo4j_uri="bolt://neo4j:7687"
            )  # fre-375-allow: tests the -eval guard itself

    def test_raises_when_eval_profile_with_prod_elasticsearch_url(self) -> None:
        """ValidationError raised when eval profile + Elasticsearch on production's hostname."""
        with pytest.raises(ValidationError, match="FRE-1372"):
            make_config(
                elasticsearch_url="http://elasticsearch:9200"  # fre-375-allow: tests the -eval guard itself
            )

    def test_raises_when_eval_profile_with_prod_database_url(self) -> None:
        """ValidationError raised when eval profile + Postgres on production's hostname."""
        with pytest.raises(ValidationError, match="FRE-1372"):
            make_config(
                database_url="postgresql+asyncpg://seshat_app:pw@postgres:5432/personal_agent"  # fre-375-allow: tests the -eval guard itself
            )

    def test_raises_when_eval_profile_with_prod_database_admin_url(self) -> None:
        """ValidationError raised when eval profile + admin URL on production's hostname."""
        with pytest.raises(ValidationError, match="FRE-1372"):
            make_config(
                database_admin_url="postgresql+asyncpg://agent:pw@postgres:5432/personal_agent"  # fre-375-allow: tests the -eval guard itself
            )

    def test_error_message_names_offending_field(self) -> None:
        """Error message names the offending field for actionability."""
        with pytest.raises(ValidationError) as exc_info:
            make_config(
                neo4j_uri="bolt://neo4j:7687"
            )  # fre-375-allow: tests the -eval guard itself
        assert "neo4j_uri" in str(exc_info.value)

    def test_raises_with_all_offenders_named(self) -> None:
        """Multiple offenders are all named in one error."""
        with pytest.raises(ValidationError) as exc_info:
            make_config(
                neo4j_uri="bolt://neo4j:7687",  # fre-375-allow: tests the -eval guard itself
                elasticsearch_url="http://elasticsearch:9200",  # fre-375-allow: tests the -eval guard itself
            )
        message = str(exc_info.value)
        assert "neo4j_uri" in message
        assert "elasticsearch_url" in message


class TestValidatorSilentForNonEvalProfile:
    """Validator does not fire for local/cloud deployment profiles."""

    @pytest.mark.parametrize("profile", ["local", "cloud"])
    def test_no_raise_for_non_eval_profile_with_prod_hosts(self, profile: str) -> None:
        """No error for a non-eval profile regardless of host (this validator's scope)."""
        cfg = make_config(
            deployment_profile=profile,
            neo4j_uri="bolt://neo4j:7687",  # fre-375-allow: out of this guard's scope
        )
        assert cfg.deployment_profile == profile


class TestValidatorSilentForLoopbackHosts:
    """Validator is silent for loopback hosts, even under deployment_profile="eval".

    A real eval-gateway container never reaches a *working* substrate over its own
    loopback (it addresses siblings by compose service name) — loopback here is
    either a broken config (this guard's scope is a live contamination path, not a
    connection-refused error) or, as here, the test suite's own ad hoc
    ``AppConfig`` construction against the FRE-375 test stack for something
    unrelated to substrate isolation (e.g. required-secret enforcement,
    ``tests/personal_agent/config/test_config_guard_startup.py``).
    """

    def test_no_raise_for_eval_profile_with_loopback_neo4j(self) -> None:
        """No error when Neo4j uses the FRE-375 test-stack loopback port."""
        cfg = make_config(neo4j_uri="bolt://localhost:7688")
        assert cfg.deployment_profile == "eval"

    def test_no_raise_for_eval_profile_with_all_loopback_hosts(self) -> None:
        """No error when every field uses a loopback host, matching the test suite's
        own `_TEST_SAFE_URLS`-style fixtures elsewhere in this config test package.
        """
        cfg = make_config(
            neo4j_uri="bolt://localhost:7688",
            elasticsearch_url="http://localhost:9201",
            database_url="postgresql+asyncpg://agent:pw@localhost:5433/personal_agent_test",
            database_admin_url="postgresql+asyncpg://agent:pw@localhost:5433/personal_agent",
        )
        assert cfg.deployment_profile == "eval"


class TestValidatorSilentForEvalHosts:
    """Validator is silent when every store resolves to an `-eval` host."""

    def test_no_raise_for_eval_profile_with_eval_hosts(self) -> None:
        """No error when all four fields use `-eval` hosts."""
        cfg = make_config()
        assert cfg.deployment_profile == "eval"
