"""Unit tests for AppConfig._validate_owner_storage_allowlist (ADR-0112 AC-1).

Verifies that the model validator raises ValidationError whenever
substrate_profile=="private" and a store resolves off the owner allowlist —
regardless of `environment` — and is silent otherwise.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from personal_agent.config.settings import AppConfig

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_SAFE_URLS: dict[str, object] = {
    "substrate_profile": "private",
    # Non-prod-fingerprint ports: these tests run under environment=TEST (set by
    # tests/conftest.py), and the pre-existing FRE-375 guard
    # (_validate_substrate_isolation) independently raises on prod-fingerprint
    # ports in TEST env. Using the test-stack ports here keeps that unrelated
    # guard silent so these tests exercise only _validate_owner_storage_allowlist.
    "neo4j_uri": "bolt://localhost:7688",
    "elasticsearch_url": "http://localhost:9201",
    "database_url": "postgresql+asyncpg://agent:pw@localhost:5433/personal_agent",
    "database_admin_url": "postgresql+asyncpg://agent:pw@localhost:5433/personal_agent",
    "sysgraph_database_url": "postgresql+asyncpg://agent:pw@localhost:5433/personal_agent",
}


def make_config(**overrides: object) -> AppConfig:
    """Build an AppConfig bypassing env-file loading.

    Starts from *_SAFE_URLS* (all fields on loopback, profile "private") and
    applies *overrides* on top.

    Args:
        **overrides: Field name → value overrides.

    Returns:
        Validated AppConfig instance.
    """
    data: dict[str, object] = {**_SAFE_URLS, **overrides}
    return AppConfig.model_validate(data)


# ---------------------------------------------------------------------------
# Guard fires — private profile + off-allowlist host
# ---------------------------------------------------------------------------


class TestValidatorRaises:
    """Validator raises ValidationError for an off-allowlist store in the private profile."""

    def test_raises_for_off_allowlist_database_url(self) -> None:
        """ValidationError raised when database_url resolves off the allowlist."""
        with pytest.raises(ValidationError, match="owner allowlist"):
            make_config(
                database_url="postgresql+asyncpg://u:p@db.managed-provider.example.com:5432/db"
            )

    def test_raises_for_off_allowlist_database_admin_url(self) -> None:
        """ValidationError raised when database_admin_url resolves off the allowlist."""
        with pytest.raises(ValidationError, match="owner allowlist"):
            make_config(
                database_admin_url="postgresql+asyncpg://u:p@db.managed-provider.example.com:5432/db"
            )

    def test_raises_for_off_allowlist_sysgraph_database_url(self) -> None:
        """ValidationError raised when sysgraph_database_url resolves off the allowlist."""
        with pytest.raises(ValidationError, match="owner allowlist"):
            make_config(
                sysgraph_database_url="postgresql+asyncpg://u:p@db.managed-provider.example.com:5432/db"
            )

    def test_raises_for_off_allowlist_neo4j_uri(self) -> None:
        """ValidationError raised when neo4j_uri resolves off the allowlist."""
        with pytest.raises(ValidationError, match="owner allowlist"):
            make_config(neo4j_uri="bolt://neo4j.managed-provider.example.com:7687")

    def test_raises_for_off_allowlist_elasticsearch_url(self) -> None:
        """ValidationError raised when elasticsearch_url resolves off the allowlist."""
        with pytest.raises(ValidationError, match="owner allowlist"):
            make_config(elasticsearch_url="https://es.managed-provider.example.com:9200")

    def test_raises_for_multiple_off_allowlist_stores(self) -> None:
        """ValidationError raised, naming every offending field, for multiple violations."""
        with pytest.raises(ValidationError) as exc_info:
            make_config(
                neo4j_uri="bolt://neo4j.managed-provider.example.com:7687",
                elasticsearch_url="https://es.managed-provider.example.com:9200",
            )
        message = str(exc_info.value)
        assert "neo4j_uri" in message
        assert "elasticsearch_url" in message

    def test_error_message_names_offending_field(self) -> None:
        """Error message references the offending field for actionability."""
        with pytest.raises(ValidationError) as exc_info:
            make_config(elasticsearch_url="https://es.managed-provider.example.com:9200")
        assert "elasticsearch_url" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Guard silent — allowlisted hosts, loopback, non-private profile
# ---------------------------------------------------------------------------


class TestValidatorSilentForAllowlistedHosts:
    """Validator is silent when stores resolve to the default allowlisted hosts."""

    def test_no_raise_for_default_docker_service_names(self) -> None:
        """The default owner_storage_allowlist covers the deployed compose service names."""
        cfg = make_config(
            database_url="postgresql+asyncpg://agent:pw@postgres:5432/personal_agent",
            database_admin_url="postgresql+asyncpg://agent:pw@postgres:5432/personal_agent",
            sysgraph_database_url="postgresql+asyncpg://agent:pw@postgres:5432/personal_agent",
            neo4j_uri="bolt://neo4j:7687",
            elasticsearch_url="http://elasticsearch:9200",
        )
        assert cfg.substrate_profile == "private"


class TestValidatorSilentForLoopback:
    """Validator is silent for loopback hosts regardless of owner_storage_allowlist."""

    def test_no_raise_for_localhost_with_empty_allowlist(self) -> None:
        """Loopback URIs pass even with an empty owner_storage_allowlist."""
        cfg = make_config(owner_storage_allowlist=[])
        assert cfg.owner_storage_allowlist == []


class TestValidatorSilentForNonPrivateProfile:
    """Validator does not fire outside the private substrate profile."""

    def test_no_raise_for_managed_profile_with_off_allowlist_host(self) -> None:
        """No error for substrate_profile='managed', regardless of host."""
        cfg = make_config(
            substrate_profile="managed",
            elasticsearch_url="https://es.managed-provider.example.com:9200",
        )
        assert cfg.substrate_profile == "managed"

    def test_no_raise_for_test_profile_with_off_allowlist_host(self) -> None:
        """No error for substrate_profile='test', regardless of host."""
        cfg = make_config(
            substrate_profile="test",
            neo4j_uri="bolt://neo4j.managed-provider.example.com:7687",
        )
        assert cfg.substrate_profile == "test"


class TestValidatorCustomAllowlist:
    """A custom owner_storage_allowlist can allow additional declared hosts."""

    def test_custom_hostname_entry_passes(self) -> None:
        """Declaring an extra hostname makes an otherwise-failing config pass."""
        cfg = make_config(
            elasticsearch_url="https://es.owned-vps.example.com:9200",
            owner_storage_allowlist=[
                "postgres",
                "neo4j",
                "elasticsearch",
                "es.owned-vps.example.com",
            ],
        )
        assert cfg.elasticsearch_url == "https://es.owned-vps.example.com:9200"

    def test_custom_cidr_entry_passes(self) -> None:
        """Declaring a CIDR range makes an IP within it pass."""
        cfg = make_config(
            neo4j_uri="bolt://10.20.0.5:7687",
            owner_storage_allowlist=["postgres", "neo4j", "elasticsearch", "10.20.0.0/16"],
        )
        assert cfg.neo4j_uri == "bolt://10.20.0.5:7687"


class TestValidatorDefaultTestSuiteConfigPasses:
    """A bare AppConfig() under the real test-suite environment never raises."""

    def test_bare_app_config_construction_does_not_raise(self) -> None:
        """conftest.py sets AGENT_SUBSTRATE_PROFILE=test, so this must not raise."""
        cfg = AppConfig()
        assert cfg.substrate_profile == "test"
