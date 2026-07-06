"""Unit tests for the owner-controlled-host allowlist helper (ADR-0112 AC-1).

These functions are pure and synchronous — no asyncio, no markers, no mocking.
Covers ``is_owner_controlled_host``: loopback, exact hostname match, CIDR-range
match, and the fail-closed cases (no implicit "any private IP" bypass, malformed
URIs).
"""

from __future__ import annotations

from personal_agent.config._owner_host_allowlist import is_owner_controlled_host

# =============================================================================
# Loopback — always allowed regardless of the allowlist
# =============================================================================


class TestLoopbackAlwaysAllowed:
    """Loopback hosts pass regardless of the declared allowlist."""

    def test_localhost_passes_with_empty_allowlist(self) -> None:
        """Localhost is always owner-controlled, even with no declared entries."""
        assert is_owner_controlled_host("postgresql://user:pw@localhost:5432/db", []) is True

    def test_127_0_0_1_passes_with_empty_allowlist(self) -> None:
        """127.0.0.1 is treated the same as localhost."""
        assert is_owner_controlled_host("bolt://127.0.0.1:7687", []) is True

    def test_ipv6_loopback_passes(self) -> None:
        """Bracketed IPv6 loopback [::1] is recognized as loopback."""
        assert is_owner_controlled_host("bolt://[::1]:7687", []) is True


# =============================================================================
# Exact hostname allowlist match
# =============================================================================


class TestExactHostnameMatch:
    """Declared exact hostnames are owner-controlled."""

    def test_declared_hostname_passes(self) -> None:
        """A host literally on the allowlist passes."""
        assert (
            is_owner_controlled_host(
                "postgresql://user:pw@postgres:5432/db", ["postgres", "neo4j", "elasticsearch"]
            )
            is True
        )

    def test_hostname_match_is_case_insensitive(self) -> None:
        """Hostname comparison is case-insensitive."""
        assert is_owner_controlled_host("http://ElasticSearch:9200", ["elasticsearch"]) is True

    def test_undeclared_hostname_fails(self) -> None:
        """A host not on the allowlist and not loopback fails."""
        assert is_owner_controlled_host("http://elasticsearch:9200", ["postgres"]) is False

    def test_provider_hostname_fails(self) -> None:
        """A managed-provider-looking hostname fails when not declared."""
        assert (
            is_owner_controlled_host(
                "postgresql://user:pw@db.managed-provider.example.com:5432/db",
                ["postgres", "neo4j", "elasticsearch"],
            )
            is False
        )


# =============================================================================
# CIDR-range allowlist match
# =============================================================================


class TestCidrRangeMatch:
    """Declared CIDR ranges are owner-controlled for IPs within them."""

    def test_ip_within_declared_cidr_passes(self) -> None:
        """An IP inside a declared private-IP range passes."""
        assert is_owner_controlled_host("bolt://10.0.5.12:7687", ["10.0.0.0/8"]) is True

    def test_ip_outside_declared_cidr_fails(self) -> None:
        """An IP outside every declared CIDR range fails."""
        assert is_owner_controlled_host("bolt://192.168.1.5:7687", ["10.0.0.0/8"]) is False

    def test_private_ip_with_no_declared_range_fails(self) -> None:
        """A private IP is NOT auto-allowed — a CIDR range must be explicitly declared."""
        assert is_owner_controlled_host("bolt://10.0.5.12:7687", []) is False
        assert is_owner_controlled_host("bolt://192.168.1.5:7687", ["postgres"]) is False

    def test_hostname_is_not_treated_as_cidr(self) -> None:
        """A non-IP host is compared as a literal string, never parsed as a network."""
        assert is_owner_controlled_host("http://elasticsearch:9200", ["10.0.0.0/8"]) is False


# =============================================================================
# Fail-closed on malformed / hostless input
# =============================================================================


class TestFailsClosedOnMalformedInput:
    """Malformed or hostless URIs never raise — they fail closed (return False)."""

    def test_empty_string_returns_false(self) -> None:
        """Empty string must not raise — it returns False."""
        assert is_owner_controlled_host("", ["postgres"]) is False

    def test_hostless_uri_returns_false(self) -> None:
        """A URI with no netloc/host returns False rather than raising."""
        assert is_owner_controlled_host("not-a-uri", ["postgres"]) is False
