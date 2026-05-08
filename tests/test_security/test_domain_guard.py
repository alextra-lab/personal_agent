"""Unit tests for DomainGuard — FRE-225.

All tests run without network access or a running agent.
The five acceptance-criteria cases:
1. Allowed URL (not in blocklist)
2. Blocklisted domain (exact match)
3. Blocklisted subdomain (parent-domain match)
4. Allowlist mode (only listed domains pass)
5. Feed-unavailable fallback (URLhaus down → bundled list)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.security import (
    DomainGuard,
    GuardMode,
    GuardResult,
    _BUNDLED_BLOCKLIST,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _guard(
    tmp_path: Path,
    *,
    mode: GuardMode = GuardMode.BLOCKLIST,
    blocklist: frozenset[str] = frozenset({"evil.com", "phish.net"}),
    allowlist: frozenset[str] = frozenset(),
    ttl_seconds: float = 3600.0,
) -> DomainGuard:
    """Return a DomainGuard pre-loaded with a synthetic blocklist."""
    g = DomainGuard(
        cache_path=tmp_path / "blocklist.json",
        ttl_seconds=ttl_seconds,
        mode=mode,
        allowlist=allowlist,
    )
    g._blocklist = blocklist
    g._last_loaded = datetime.now(timezone.utc)
    return g


# ---------------------------------------------------------------------------
# 1. Allowed URL
# ---------------------------------------------------------------------------


class TestAllowedUrl:
    def test_safe_url_passes_blocklist_mode(self, tmp_path: Path) -> None:
        """A URL whose domain is not in the blocklist is allowed."""
        g = _guard(tmp_path)
        result = g.check_url("https://safe-domain.com/page")
        assert result.allowed is True
        assert result.reason == "not_blocked"
        assert result.matched_entry is None

    def test_guard_off_allows_everything(self, tmp_path: Path) -> None:
        """GuardMode.OFF passes all URLs unconditionally."""
        g = _guard(tmp_path, mode=GuardMode.OFF)
        result = g.check_url("https://evil.com/malware")
        assert result.allowed is True
        assert result.reason == "guard_off"


# ---------------------------------------------------------------------------
# 2. Blocklisted domain (exact match)
# ---------------------------------------------------------------------------


class TestBlocklistedDomain:
    def test_exact_domain_match_is_blocked(self, tmp_path: Path) -> None:
        """A URL whose hostname exactly matches a blocklist entry is blocked."""
        g = _guard(tmp_path)
        result = g.check_url("https://evil.com/download")
        assert result.allowed is False
        assert result.reason == "blocklist_match"
        assert result.matched_entry == "evil.com"

    def test_http_scheme_also_blocked(self, tmp_path: Path) -> None:
        """HTTP (not just HTTPS) URLs are checked."""
        g = _guard(tmp_path)
        result = g.check_url("http://phish.net/login")
        assert result.allowed is False
        assert result.matched_entry == "phish.net"

    def test_url_with_port_is_blocked(self, tmp_path: Path) -> None:
        """Port numbers don't bypass the guard."""
        g = _guard(tmp_path)
        result = g.check_url("https://evil.com:8443/payload")
        assert result.allowed is False
        assert result.matched_entry == "evil.com"


# ---------------------------------------------------------------------------
# 3. Blocklisted subdomain (parent-domain match)
# ---------------------------------------------------------------------------


class TestBlocklistedSubdomain:
    def test_subdomain_blocked_by_parent_entry(self, tmp_path: Path) -> None:
        """sub.evil.com is blocked because evil.com is in the blocklist."""
        g = _guard(tmp_path)
        result = g.check_url("https://cdn.evil.com/script.js")
        assert result.allowed is False
        assert result.reason == "blocklist_match"
        assert result.matched_entry == "evil.com"

    def test_deep_subdomain_blocked(self, tmp_path: Path) -> None:
        """a.b.evil.com is also blocked by the evil.com entry."""
        g = _guard(tmp_path)
        result = g.check_url("https://a.b.evil.com/path")
        assert result.allowed is False
        assert result.matched_entry == "evil.com"

    def test_similar_domain_not_blocked(self, tmp_path: Path) -> None:
        """notevil.com is NOT blocked just because evil.com is."""
        g = _guard(tmp_path)
        result = g.check_url("https://notevil.com/page")
        assert result.allowed is True


# ---------------------------------------------------------------------------
# 4. Allowlist mode
# ---------------------------------------------------------------------------


class TestAllowlistMode:
    def test_listed_domain_allowed(self, tmp_path: Path) -> None:
        """In allowlist mode, a domain in the allowlist passes."""
        g = _guard(
            tmp_path,
            mode=GuardMode.ALLOWLIST,
            allowlist=frozenset({"trusted.org", "api.example.com"}),
        )
        result = g.check_url("https://trusted.org/data")
        assert result.allowed is True
        assert result.reason == "allowlist_match"

    def test_subdomain_of_allowlisted_domain_passes(self, tmp_path: Path) -> None:
        """sub.trusted.org passes when trusted.org is in the allowlist."""
        g = _guard(
            tmp_path,
            mode=GuardMode.ALLOWLIST,
            allowlist=frozenset({"trusted.org"}),
        )
        result = g.check_url("https://api.trusted.org/v1/endpoint")
        assert result.allowed is True

    def test_unlisted_domain_blocked_in_allowlist_mode(self, tmp_path: Path) -> None:
        """In allowlist mode, a domain NOT in the allowlist is blocked."""
        g = _guard(
            tmp_path,
            mode=GuardMode.ALLOWLIST,
            allowlist=frozenset({"trusted.org"}),
        )
        result = g.check_url("https://untrusted-site.com/page")
        assert result.allowed is False
        assert result.reason == "not_in_allowlist"

    def test_empty_allowlist_blocks_all(self, tmp_path: Path) -> None:
        """An empty allowlist in allowlist mode blocks every URL."""
        g = _guard(tmp_path, mode=GuardMode.ALLOWLIST, allowlist=frozenset())
        result = g.check_url("https://anywhere.com")
        assert result.allowed is False


# ---------------------------------------------------------------------------
# 5. Feed-unavailable fallback
# ---------------------------------------------------------------------------


class TestFeedUnavailableFallback:
    @pytest.mark.asyncio
    async def test_uses_bundled_list_when_urlhaus_fails(self, tmp_path: Path) -> None:
        """When URLhaus is unreachable, the guard loads the bundled fallback list."""
        g = DomainGuard(
            cache_path=tmp_path / "blocklist.json",
            ttl_seconds=3600.0,
            mode=GuardMode.BLOCKLIST,
        )
        # Simulate network failure on URLhaus fetch
        with patch.object(g, "_fetch_urlhaus", new=AsyncMock(side_effect=ConnectionError("timeout"))):
            await g._refresh()

        assert g._blocklist == _BUNDLED_BLOCKLIST
        assert g._last_loaded is not None

    @pytest.mark.asyncio
    async def test_bundled_list_blocks_known_test_domain(self, tmp_path: Path) -> None:
        """After fallback load, the bundled malware test domain is blocked."""
        g = DomainGuard(
            cache_path=tmp_path / "blocklist.json",
            ttl_seconds=3600.0,
            mode=GuardMode.BLOCKLIST,
        )
        with patch.object(g, "_fetch_urlhaus", new=AsyncMock(side_effect=ConnectionError("err"))):
            await g._refresh()

        result = g.check_url("https://malware.wicar.org/test")
        assert result.allowed is False
        assert result.reason == "blocklist_match"

    @pytest.mark.asyncio
    async def test_valid_cache_skips_network(self, tmp_path: Path) -> None:
        """A fresh disk cache is used without hitting the network."""
        cache_path = tmp_path / "blocklist.json"
        cached_domains = ["evil.com", "phish.net"]
        cache_path.write_text(
            json.dumps({
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "domain_count": 2,
                "domains": cached_domains,
            })
        )

        g = DomainGuard(cache_path=cache_path, ttl_seconds=3600.0)
        fetch_mock = AsyncMock()
        with patch.object(g, "_fetch_urlhaus", new=fetch_mock):
            await g._refresh()

        fetch_mock.assert_not_called()
        assert "evil.com" in g._blocklist

    @pytest.mark.asyncio
    async def test_stale_cache_triggers_refresh(self, tmp_path: Path) -> None:
        """An expired cache triggers a fresh URLhaus fetch."""
        cache_path = tmp_path / "blocklist.json"
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        cache_path.write_text(
            json.dumps({"cached_at": stale_time, "domain_count": 1, "domains": ["old.com"]})
        )

        g = DomainGuard(cache_path=cache_path, ttl_seconds=3600.0)
        fresh_domains = {"freshly-fetched-evil.net"}
        with patch.object(g, "_fetch_urlhaus", new=AsyncMock(return_value=fresh_domains)):
            await g._refresh()

        assert "freshly-fetched-evil.net" in g._blocklist


# ---------------------------------------------------------------------------
# ensure_loaded integration
# ---------------------------------------------------------------------------


class TestEnsureLoaded:
    @pytest.mark.asyncio
    async def test_ensure_loaded_triggers_refresh_when_stale(self, tmp_path: Path) -> None:
        """ensure_loaded() triggers _refresh() when _last_loaded is None."""
        g = DomainGuard(cache_path=tmp_path / "bl.json", ttl_seconds=3600.0)
        assert g._last_loaded is None

        with patch.object(g, "_fetch_urlhaus", new=AsyncMock(return_value={"new.evil"})):
            await g.ensure_loaded()

        assert g._last_loaded is not None
        assert "new.evil" in g._blocklist

    @pytest.mark.asyncio
    async def test_ensure_loaded_skips_when_fresh(self, tmp_path: Path) -> None:
        """ensure_loaded() does nothing when the list was just refreshed."""
        g = _guard(tmp_path, blocklist=frozenset({"cached.evil"}))
        fetch_mock = AsyncMock()
        with patch.object(g, "_fetch_urlhaus", new=fetch_mock):
            await g.ensure_loaded()

        fetch_mock.assert_not_called()


# ---------------------------------------------------------------------------
# GuardResult structure
# ---------------------------------------------------------------------------


class TestGuardResult:
    def test_is_frozen(self) -> None:
        """GuardResult is immutable."""
        r = GuardResult(allowed=True, reason="not_blocked")
        with pytest.raises(Exception):
            r.allowed = False  # type: ignore[misc]
