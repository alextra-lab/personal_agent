"""Unit tests for substrate fingerprint helper functions (FRE-375).

These functions are pure and synchronous — no asyncio, no markers, no mocking.
Covers all three fingerprint detectors:
  - is_prod_neo4j_uri       (localhost:7687)
  - is_prod_elasticsearch_url (localhost:9200)
  - is_prod_postgres_url    (localhost:5432)
"""

from __future__ import annotations

from personal_agent.config._substrate_fingerprint import (
    is_prod_elasticsearch_url,
    is_prod_neo4j_uri,
    is_prod_postgres_url,
)

# =============================================================================
# is_prod_neo4j_uri
# =============================================================================


class TestIsProdNeo4jUri:
    """Tests for the Neo4j production fingerprint detector."""

    def test_canonical_prod_uri_returns_true(self) -> None:
        """bolt://localhost:7687 is the default production fingerprint."""
        assert is_prod_neo4j_uri("bolt://localhost:7687") is True

    def test_127_alias_returns_true(self) -> None:
        """127.0.0.1 is treated as equivalent to localhost."""
        assert is_prod_neo4j_uri("bolt://127.0.0.1:7687") is True

    def test_test_stack_port_returns_false(self) -> None:
        """bolt://localhost:7688 is the test-stack port — not prod."""
        assert is_prod_neo4j_uri("bolt://localhost:7688") is False

    def test_non_localhost_host_returns_false(self) -> None:
        """Remote host on the prod port is not a local prod fingerprint."""
        assert is_prod_neo4j_uri("bolt://neo4j-server.example.com:7687") is False

    def test_different_scheme_same_host_port_returns_true(self) -> None:
        """neo4j+ssc:// on localhost:7687 matches — scheme is not part of the fingerprint.

        Decision: the fingerprint is defined purely by host + port. Scheme variants
        (neo4j://, bolt+s://, neo4j+ssc://) all address the same process on the same
        port and should trigger the guard equally.
        """
        assert is_prod_neo4j_uri("neo4j+ssc://localhost:7687") is True

    def test_no_port_in_uri_returns_false(self) -> None:
        """A URI without an explicit port cannot match the prod fingerprint."""
        assert is_prod_neo4j_uri("bolt://localhost") is False

    def test_empty_string_returns_false(self) -> None:
        """Empty string must not raise — it returns False."""
        assert is_prod_neo4j_uri("") is False


# =============================================================================
# is_prod_elasticsearch_url
# =============================================================================


class TestIsProdElasticsearchUrl:
    """Tests for the Elasticsearch production fingerprint detector."""

    def test_canonical_prod_url_returns_true(self) -> None:
        """http://localhost:9200 is the default production fingerprint."""
        assert is_prod_elasticsearch_url("http://localhost:9200") is True

    def test_127_alias_returns_true(self) -> None:
        """127.0.0.1 is treated as equivalent to localhost."""
        assert is_prod_elasticsearch_url("http://127.0.0.1:9200") is True

    def test_test_stack_port_returns_false(self) -> None:
        """Port 9201 is the test-stack port — not prod."""
        assert is_prod_elasticsearch_url("http://localhost:9201") is False

    def test_non_localhost_host_returns_false(self) -> None:
        """Remote host on the prod port is not a local prod fingerprint."""
        assert is_prod_elasticsearch_url("http://es-cluster.example.com:9200") is False

    def test_different_scheme_same_host_port_returns_true(self) -> None:
        """https:// on localhost:9200 matches — scheme is not part of the fingerprint."""
        assert is_prod_elasticsearch_url("https://localhost:9200") is True

    def test_no_port_in_url_returns_false(self) -> None:
        """A URL without an explicit port cannot match the prod fingerprint."""
        assert is_prod_elasticsearch_url("http://localhost") is False

    def test_empty_string_returns_false(self) -> None:
        """Empty string must not raise — it returns False."""
        assert is_prod_elasticsearch_url("") is False


# =============================================================================
# is_prod_postgres_url
# =============================================================================


class TestIsProdPostgresUrl:
    """Tests for the PostgreSQL production fingerprint detector."""

    def test_canonical_prod_url_returns_true(self) -> None:
        """postgresql+asyncpg://...@localhost:5432/... is the default production fingerprint."""
        assert (
            is_prod_postgres_url(
                "postgresql+asyncpg://agent:pw@localhost:5432/personal_agent"
            )
            is True
        )

    def test_127_alias_returns_true(self) -> None:
        """127.0.0.1 is treated as equivalent to localhost."""
        assert (
            is_prod_postgres_url(
                "postgresql+asyncpg://agent:pw@127.0.0.1:5432/personal_agent"
            )
            is True
        )

    def test_test_stack_port_returns_false(self) -> None:
        """Port 5433 is the test-stack port — not prod."""
        assert (
            is_prod_postgres_url(
                "postgresql+asyncpg://agent:pw@localhost:5433/personal_agent"
            )
            is False
        )

    def test_non_localhost_host_returns_false(self) -> None:
        """Remote host on the prod port is not a local prod fingerprint."""
        assert (
            is_prod_postgres_url(
                "postgresql+asyncpg://agent:pw@db.example.com:5432/personal_agent"
            )
            is False
        )

    def test_different_scheme_same_host_port_returns_true(self) -> None:
        """postgresql:// (no asyncpg driver suffix) on localhost:5432 matches.

        Decision: scheme variant does not change which process is addressed.
        """
        assert is_prod_postgres_url("postgresql://agent:pw@localhost:5432/db") is True

    def test_no_port_in_url_returns_false(self) -> None:
        """A URL without an explicit port cannot match the prod fingerprint."""
        assert (
            is_prod_postgres_url(
                "postgresql+asyncpg://agent:pw@localhost/personal_agent"
            )
            is False
        )

    def test_empty_string_returns_false(self) -> None:
        """Empty string must not raise — it returns False."""
        assert is_prod_postgres_url("") is False
