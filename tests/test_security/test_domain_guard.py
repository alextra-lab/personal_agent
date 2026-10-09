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

import httpx
import pytest
import structlog

from personal_agent.security import (
    _BUNDLED_BLOCKLIST,
    DomainGuard,
    GuardMode,
    GuardResult,
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
        with patch.object(
            g, "_fetch_urlhaus", new=AsyncMock(side_effect=ConnectionError("timeout"))
        ):
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
            json.dumps(
                {
                    "cached_at": datetime.now(timezone.utc).isoformat(),
                    "domain_count": 2,
                    "domains": cached_domains,
                }
            )
        )

        g = DomainGuard(cache_path=cache_path, ttl_seconds=3600.0)
        fetch_mock = AsyncMock()
        with patch.object(g, "_fetch_urlhaus", new=fetch_mock):
            await g._refresh()

        fetch_mock.assert_not_called()
        # Superset (not equality) — _refresh()'s disk-cache branch unions the cached
        # domains with _BUNDLED_BLOCKLIST (FRE-1330), same as the network-fetch branch.
        assert g._blocklist >= frozenset(cached_domains)

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

        # Superset (not equality) — _refresh() merges fetched domains with _BUNDLED_BLOCKLIST.
        assert g._blocklist >= frozenset(fresh_domains)


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
        # Superset (not equality) — _refresh() merges fetched domains with _BUNDLED_BLOCKLIST.
        assert g._blocklist >= frozenset({"new.evil"})

    @pytest.mark.asyncio
    async def test_ensure_loaded_skips_when_fresh(self, tmp_path: Path) -> None:
        """ensure_loaded() does nothing when the list was just refreshed."""
        g = _guard(tmp_path, blocklist=frozenset({"cached.evil"}))
        fetch_mock = AsyncMock()
        with patch.object(g, "_fetch_urlhaus", new=fetch_mock):
            await g.ensure_loaded()

        fetch_mock.assert_not_called()


# ---------------------------------------------------------------------------
# note_staleness (FRE-1162) — the request-path freshness signal that never fetches
# ---------------------------------------------------------------------------


class TestNoteStaleness:
    def test_logs_once_when_stale_and_blocklist_mode(self, tmp_path: Path) -> None:
        """A never-loaded guard is stale; note_staleness() logs a warning."""
        g = DomainGuard(cache_path=tmp_path / "bl.json", mode=GuardMode.BLOCKLIST)
        assert g._last_loaded is None

        with patch("personal_agent.security.log") as mock_log:
            g.note_staleness()

        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args.args[0] == "domain_guard_stale_on_request_path"

    def test_noop_when_off_mode(self, tmp_path: Path) -> None:
        """GuardMode.OFF never logs staleness — it never consults the blocklist at all."""
        g = DomainGuard(cache_path=tmp_path / "bl.json", mode=GuardMode.OFF)
        assert g._last_loaded is None

        with patch("personal_agent.security.log") as mock_log:
            g.note_staleness()

        mock_log.warning.assert_not_called()

    def test_silent_when_fresh(self, tmp_path: Path) -> None:
        """A recently-loaded guard is not stale — note_staleness() logs nothing."""
        g = _guard(tmp_path)  # _guard() sets _last_loaded = now()

        with patch("personal_agent.security.log") as mock_log:
            g.note_staleness()

        mock_log.warning.assert_not_called()

    def test_does_not_log_twice_for_same_staleness_episode(self, tmp_path: Path) -> None:
        """Repeated calls during one staleness episode log only once — no per-request flood."""
        g = DomainGuard(cache_path=tmp_path / "bl.json", mode=GuardMode.BLOCKLIST)

        with patch("personal_agent.security.log") as mock_log:
            g.note_staleness()
            g.note_staleness()
            g.note_staleness()

        mock_log.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_resets_after_refresh(self, tmp_path: Path) -> None:
        """A fresh staleness episode after a successful refresh logs again.

        Proves the ``_logged_stale`` throttle actually resets on reload, not just
        that a freshly-loaded guard stays quiet (that's test_silent_when_fresh).
        """
        g = DomainGuard(
            cache_path=tmp_path / "bl.json", mode=GuardMode.BLOCKLIST, ttl_seconds=3600.0
        )

        with patch("personal_agent.security.log") as mock_log:
            g.note_staleness()  # 1st staleness episode
        assert mock_log.warning.call_count == 1

        with patch.object(g, "_fetch_urlhaus", new=AsyncMock(return_value={"new.evil"})):
            await g._refresh()

        # Force a second staleness episode by pushing _last_loaded back past the TTL.
        g._last_loaded = datetime.now(timezone.utc) - timedelta(seconds=g._ttl + 1)

        with patch("personal_agent.security.log") as mock_log2:
            g.note_staleness()

        mock_log2.warning.assert_called_once()


# ---------------------------------------------------------------------------
# GuardResult structure
# ---------------------------------------------------------------------------


class TestGuardResult:
    def test_is_frozen(self) -> None:
        """GuardResult is immutable."""
        r = GuardResult(allowed=True, reason="not_blocked")
        with pytest.raises(Exception):
            r.allowed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 6. FRE-1330: the named Alibaba bucket is in the bundled blocklist, and the
#    bundled list survives a disk-cache load (not just a fresh __init__ default)
# ---------------------------------------------------------------------------


class TestFre1330NamedBucketBlock:
    def test_ticket_url_is_blocked_by_a_fresh_guard(self) -> None:
        """A freshly-constructed guard (no ensure_loaded() call) already blocks this —
        _blocklist starts as _BUNDLED_BLOCKLIST in __init__.
        """
        g = DomainGuard()
        result = g.check_url(
            "https://routify-file-proxy-sg.oss-ap-southeast-1.aliyuncs.com/proxy_temp_file/"
            "production/2026-08-30/trace_x/requestId_y/hash?Expires=1818630439"
        )
        assert result.allowed is False
        assert result.reason == "blocklist_match"
        assert result.matched_entry == "routify-file-proxy-sg.oss-ap-southeast-1.aliyuncs.com"

    @pytest.mark.asyncio
    async def test_bundled_entry_survives_a_stale_predating_disk_cache(
        self, tmp_path: Path
    ) -> None:
        """A disk cache written before this bundled entry existed (e.g. a prior deploy's
        URLhaus fetch) must not silently drop the new block on the next warm-reload.

        Regression for the pre-existing _refresh() bug this ticket's AC-3 exposed: the
        disk-cache branch replaced _blocklist with the cached set alone, without
        unioning _BUNDLED_BLOCKLIST the way the network-fetch branch already did.
        """
        cache_path = tmp_path / "blocklist.json"
        cache_path.write_text(
            json.dumps(
                {
                    "cached_at": datetime.now(timezone.utc).isoformat(),
                    "domain_count": 1,
                    # Deliberately does NOT include the FRE-1330 bundled entry.
                    "domains": ["some-old-urlhaus-domain.example"],
                }
            )
        )

        g = DomainGuard(cache_path=cache_path, ttl_seconds=3600.0)
        await g._refresh()

        result = g.check_url("https://routify-file-proxy-sg.oss-ap-southeast-1.aliyuncs.com/x")
        assert result.allowed is False
        assert result.reason == "blocklist_match"


# ---------------------------------------------------------------------------
# 7. FRE-1552: a shared platform is blocked per malicious URL, not per hostname
# ---------------------------------------------------------------------------

_FEED_SAMPLE = "\n".join(
    [
        "################ URLhaus ################",
        "# url",
        "https://github.com/badactor/repo/releases/download/x/payload.exe",
        "https://raw.githubusercontent.com/badactor/repo/main/dropper.sh",
        "http://203.0.113.9:8080/bin.sh",
        "http://dedicated-malware.example/a.exe",
        "http://dedicated-malware.example/other/b.exe",
        "",
    ]
)


async def _guard_loaded_from_feed(tmp_path: Path, feed_text: str = _FEED_SAMPLE) -> DomainGuard:
    """Return a guard whose blocklist came from parsing *feed_text* via ``_fetch_urlhaus``."""
    g = DomainGuard(cache_path=tmp_path / "blocklist.json", ttl_seconds=3600.0)
    response = httpx.Response(200, text=feed_text, request=httpx.Request("GET", "http://feed"))
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)):
        await g._refresh()
    return g


class TestFre1552SharedPlatformUrlLevelBlock:
    @pytest.mark.asyncio
    async def test_ac1_listed_url_blocked_and_platform_page_allowed(self, tmp_path: Path) -> None:
        """AC-1: the exact listed github.com URL is blocked; a releases page is not."""
        g = await _guard_loaded_from_feed(tmp_path)

        listed = g.check_url("https://github.com/badactor/repo/releases/download/x/payload.exe")
        assert listed.allowed is False

        page = g.check_url("https://github.com/vllm-project/vllm/releases")
        assert page.allowed is True

    @pytest.mark.asyncio
    async def test_other_github_hosts_stay_reachable(self, tmp_path: Path) -> None:
        """api.github.com and a different raw.githubusercontent.com path are allowed."""
        g = await _guard_loaded_from_feed(tmp_path)

        assert g.check_url("https://api.github.com/repos/ggml-org/llama.cpp/releases").allowed
        assert g.check_url(
            "https://raw.githubusercontent.com/ggml-org/llama.cpp/master/R.md"
        ).allowed
        listed = g.check_url("https://raw.githubusercontent.com/badactor/repo/main/dropper.sh")
        assert listed.allowed is False

    @pytest.mark.asyncio
    async def test_listed_url_blocked_across_scheme_fragment_and_default_port(
        self, tmp_path: Path
    ) -> None:
        """The same resource over http, with a fragment, or on port 443 is still blocked."""
        g = await _guard_loaded_from_feed(tmp_path)

        for variant in (
            "http://github.com/badactor/repo/releases/download/x/payload.exe",
            "https://GitHub.com/badactor/repo/releases/download/x/payload.exe#frag",
            "https://github.com:443/badactor/repo/releases/download/x/payload.exe",
        ):
            assert g.check_url(variant).allowed is False, variant

    @pytest.mark.asyncio
    async def test_ac2_dedicated_hosts_stay_blocked_for_every_path(self, tmp_path: Path) -> None:
        """AC-2: a dedicated hostname and a bare IP are blocked on paths the feed never listed."""
        g = await _guard_loaded_from_feed(tmp_path)

        assert g.check_url("http://dedicated-malware.example/never/listed.txt").allowed is False
        assert g.check_url("http://dedicated-malware.example/").allowed is False
        assert g.check_url("http://203.0.113.9:8080/other-path").allowed is False

    @pytest.mark.asyncio
    async def test_fresh_legacy_cache_loses_the_platform_host_but_keeps_dedicated_hosts(
        self, tmp_path: Path
    ) -> None:
        """A fresh cache written before FRE-1552 holds the bare host ``github.com``.

        Only that platform entry is dropped. The dedicated hosts stay blocked, and the
        guard does not depend on a feed fetch to keep them: a fresh cache never fetches.
        """
        cache_path = tmp_path / "blocklist.json"
        cache_path.write_text(
            json.dumps(
                {
                    "cached_at": datetime.now(timezone.utc).isoformat(),
                    "domain_count": 3,
                    "domains": ["github.com", "dedicated-malware.example", "203.0.113.9"],
                }
            )
        )
        g = DomainGuard(cache_path=cache_path, ttl_seconds=3600.0)
        fetch_mock = AsyncMock(side_effect=ConnectionError("feed down"))
        with patch.object(g, "_fetch_urlhaus", new=fetch_mock):
            await g._refresh()

        fetch_mock.assert_not_awaited()
        assert g.check_url("https://github.com/vllm-project/vllm/releases").allowed is True
        assert g.check_url("http://dedicated-malware.example/any/path").allowed is False
        assert g.check_url("http://203.0.113.9/x").allowed is False

    @pytest.mark.asyncio
    async def test_cache_round_trip_keeps_url_entries(self, tmp_path: Path) -> None:
        """A second guard that loads the saved cache blocks the same URL and allows the page."""
        await _guard_loaded_from_feed(tmp_path)

        g2 = DomainGuard(cache_path=tmp_path / "blocklist.json", ttl_seconds=3600.0)
        with patch.object(g2, "_fetch_urlhaus", new=AsyncMock()) as fetch_mock:
            await g2._refresh()

        fetch_mock.assert_not_called()
        listed = g2.check_url("https://github.com/badactor/repo/releases/download/x/payload.exe")
        assert listed.allowed is False
        assert g2.check_url("https://github.com/vllm-project/vllm/releases").allowed is True


class TestFre1552FeedKeyMatchesWhatHttpxSends:
    @pytest.mark.asyncio
    async def test_feed_line_httpx_would_rewrite_is_still_blocked(self, tmp_path: Path) -> None:
        """The request hook sees httpx's normalised URL; the feed key must match that form.

        A feed line with a space or a dot segment is sent as ``a%20b.exe`` / without the
        ``..`` segment. If the key keeps the raw spelling, the listed URL stops being blocked.
        """
        feed = "\n".join(
            [
                "https://github.com/bad/repo/a b.exe",
                "https://github.com/bad/x/../repo/c.exe",
            ]
        )
        g = await _guard_loaded_from_feed(tmp_path, feed)

        for listed in ("https://github.com/bad/repo/a b.exe", "https://github.com/bad/repo/c.exe"):
            sent = str(httpx.Request("GET", listed).url)
            assert g.check_url(sent).allowed is False, sent
            assert g.check_url(listed).allowed is False, listed


class TestFre1552EquivalentSpellings:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "variant",
        [
            "https://github.com/bad/a%2fb/x.exe",  # lowercase escape of a reserved character
            "https://github.com/bad/%61/x.exe",  # escaped unreserved character
            "https://github.com/bad/a/x.exe?",  # empty query
            "https://github.com./bad/a/x.exe",  # fully qualified host
            "https://GITHUB.com/bad/a/x.exe",
        ],
    )
    async def test_equivalent_spellings_of_a_listed_url_stay_blocked(
        self, tmp_path: Path, variant: str
    ) -> None:
        """Spellings that name the same octets or the same host as a listed URL are blocked."""
        feed = "https://github.com/bad/a%2Fb/x.exe\nhttps://github.com/bad/a/x.exe\n"
        g = await _guard_loaded_from_feed(tmp_path, feed)

        assert g.check_url(variant).allowed is False, variant
        assert g.check_url(str(httpx.Request("GET", variant).url)).allowed is False, variant

    @pytest.mark.asyncio
    async def test_unicode_dot_in_a_feed_host_still_names_the_platform(
        self, tmp_path: Path
    ) -> None:
        """A feed line spelling the host with a fullwidth dot is the same URL once httpx sends it."""
        g = await _guard_loaded_from_feed(tmp_path, "https://github\u3002com/bad/x.exe\n")

        assert g.check_url("https://github.com/bad/x.exe").allowed is False
        assert g.check_url("https://github.com/vllm-project/vllm/releases").allowed is True

    @pytest.mark.asyncio
    async def test_idn_dedicated_host_is_blocked_as_httpx_sends_it(self, tmp_path: Path) -> None:
        """An IDN host from the feed matches the punycode host the request hook sees."""
        g = await _guard_loaded_from_feed(tmp_path, "http://b\u00fccher.example/x\n")

        sent = str(httpx.Request("GET", "http://b\u00fccher.example/other").url)
        assert g.check_url(sent).allowed is False


# ---------------------------------------------------------------------------
# FRE-1560 — a failed refresh keeps the best list available
# ---------------------------------------------------------------------------


def _write_cache(path: Path, domains: list[str], age: timedelta) -> None:
    """Write a disk cache that was cached *age* ago."""
    cached_at = (datetime.now(timezone.utc) - age).isoformat()
    path.write_text(
        json.dumps({"cached_at": cached_at, "domain_count": len(domains), "domains": domains})
    )


def _event(logs: list[dict[str, object]], name: str) -> dict[str, object]:
    """Return the single captured log entry named *name*; fail if there is not exactly one."""
    matches = [entry for entry in logs if entry["event"] == name]
    assert len(matches) == 1, [entry["event"] for entry in logs]
    return matches[0]


class TestFre1560FailedRefreshKeepsBestList:
    @pytest.mark.asyncio
    async def test_ac1_failed_fetch_keeps_the_in_memory_list(self, tmp_path: Path) -> None:
        """A list loaded from the feed survives a later fetch that raises."""
        g = DomainGuard(cache_path=tmp_path / "blocklist.json", ttl_seconds=3600.0)
        with patch.object(g, "_fetch_urlhaus", new=AsyncMock(return_value={"feed-evil.net"})):
            await g._refresh()
        assert g.check_url("https://feed-evil.net/x").allowed is False

        # Expire the TTL and the disk cache, so only the in-memory list can answer.
        g._last_loaded = datetime.now(timezone.utc) - timedelta(hours=2)
        _write_cache(tmp_path / "blocklist.json", ["other.net"], timedelta(hours=2))
        failing = AsyncMock(side_effect=ConnectionError("down"))
        with (
            patch.object(g, "_fetch_urlhaus", new=failing),
            structlog.testing.capture_logs() as logs,
        ):
            await g._refresh()

        assert g.check_url("https://feed-evil.net/x").allowed is False
        assert g._blocklist >= _BUNDLED_BLOCKLIST
        event = _event(logs, "domain_guard_using_fallback")
        assert event["source"] == "memory"
        assert isinstance(event["age_seconds"], float)
        assert event["age_seconds"] == pytest.approx(0, abs=60)  # the list is seconds old

    @pytest.mark.asyncio
    async def test_a_second_failed_refresh_still_reports_the_age_of_the_list(
        self, tmp_path: Path
    ) -> None:
        """The age counts from when the list was produced, not from the last failed attempt."""
        g = DomainGuard(cache_path=tmp_path / "blocklist.json", ttl_seconds=3600.0)
        with patch.object(g, "_fetch_urlhaus", new=AsyncMock(return_value={"feed-evil.net"})):
            await g._refresh()
        g._blocklist_as_of = datetime.now(timezone.utc) - timedelta(hours=5)
        _write_cache(tmp_path / "blocklist.json", ["other.net"], timedelta(hours=5))

        failing = AsyncMock(side_effect=ConnectionError("down"))
        for _ in range(2):
            with (
                patch.object(g, "_fetch_urlhaus", new=failing),
                structlog.testing.capture_logs() as logs,
            ):
                await g._refresh()
            event = _event(logs, "domain_guard_using_fallback")
            assert event["source"] == "memory"
            assert event["age_seconds"] == pytest.approx(5 * 3600, abs=60)

    @pytest.mark.asyncio
    async def test_ac2_cold_start_uses_the_stale_disk_cache(self, tmp_path: Path) -> None:
        """With nothing in memory, a cache older than the TTL still feeds the guard."""
        cache_path = tmp_path / "blocklist.json"
        _write_cache(cache_path, ["old-evil.net"], timedelta(hours=2))
        g = DomainGuard(cache_path=cache_path, ttl_seconds=3600.0)

        with (
            patch.object(g, "_fetch_urlhaus", new=AsyncMock(side_effect=ConnectionError("down"))),
            structlog.testing.capture_logs() as logs,
        ):
            await g._refresh()

        assert g.check_url("https://old-evil.net/x").allowed is False
        assert g._blocklist >= _BUNDLED_BLOCKLIST
        event = _event(logs, "domain_guard_using_fallback")
        assert event["source"] == "stale_cache"
        assert event["age_seconds"] == pytest.approx(2 * 3600, abs=60)

    @pytest.mark.asyncio
    async def test_stale_cache_fallback_drops_a_pre_fre1552_platform_host(
        self, tmp_path: Path
    ) -> None:
        """The stale cache keeps dedicated hosts but not a whole shared platform."""
        cache_path = tmp_path / "blocklist.json"
        _write_cache(cache_path, ["github.com", "old-evil.net"], timedelta(hours=2))
        g = DomainGuard(cache_path=cache_path, ttl_seconds=3600.0)

        with patch.object(g, "_fetch_urlhaus", new=AsyncMock(side_effect=ConnectionError("down"))):
            await g._refresh()

        assert g.check_url("https://old-evil.net/x").allowed is False
        assert g.check_url("https://github.com/vllm-project/vllm").allowed is True

    @pytest.mark.asyncio
    async def test_no_memory_and_no_cache_falls_back_to_bundled(self, tmp_path: Path) -> None:
        """With no feed list and no cache file, the bundled list is the last resort."""
        g = DomainGuard(cache_path=tmp_path / "blocklist.json", ttl_seconds=3600.0)

        with (
            patch.object(g, "_fetch_urlhaus", new=AsyncMock(side_effect=ConnectionError("down"))),
            structlog.testing.capture_logs() as logs,
        ):
            await g._refresh()

        assert g._blocklist == _BUNDLED_BLOCKLIST
        event = _event(logs, "domain_guard_using_fallback")
        assert event["source"] == "bundled"
        assert event["age_seconds"] is None

    @pytest.mark.asyncio
    async def test_unreadable_stale_cache_falls_back_to_bundled(self, tmp_path: Path) -> None:
        """A corrupt cache file is not a source: the bundled list answers."""
        cache_path = tmp_path / "blocklist.json"
        cache_path.write_text("{not json")
        g = DomainGuard(cache_path=cache_path, ttl_seconds=3600.0)

        with (
            patch.object(g, "_fetch_urlhaus", new=AsyncMock(side_effect=ConnectionError("down"))),
            structlog.testing.capture_logs() as logs,
        ):
            await g._refresh()

        assert g._blocklist == _BUNDLED_BLOCKLIST
        assert _event(logs, "domain_guard_using_fallback")["source"] == "bundled"

    @pytest.mark.asyncio
    async def test_ac3_failed_cache_write_keeps_the_fetched_list(self, tmp_path: Path) -> None:
        """A write error after a good fetch must not replace the list with the bundled one."""
        g = DomainGuard(cache_path=tmp_path / "blocklist.json", ttl_seconds=3600.0)

        with (
            patch.object(g, "_fetch_urlhaus", new=AsyncMock(return_value={"feed-evil.net"})),
            patch.object(g, "_save_to_disk_cache", side_effect=OSError("disk full")),
            structlog.testing.capture_logs() as logs,
        ):
            await g._refresh()

        assert g.check_url("https://feed-evil.net/x").allowed is False
        assert g._blocklist >= _BUNDLED_BLOCKLIST
        event = _event(logs, "domain_guard_cache_write_failed")
        assert "disk full" in str(event["error"])
        assert not [e for e in logs if e["event"] == "domain_guard_using_fallback"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("feed_text", ["", "# only a comment\n\n"])
    async def test_an_empty_feed_response_keeps_the_list_and_the_cache(
        self, tmp_path: Path, feed_text: str
    ) -> None:
        """A 200 response with no entries is a failed fetch, not an empty blocklist."""
        g = DomainGuard(cache_path=tmp_path / "blocklist.json", ttl_seconds=3600.0)
        with patch.object(g, "_fetch_urlhaus", new=AsyncMock(return_value={"feed-evil.net"})):
            await g._refresh()
        cache_before = (tmp_path / "blocklist.json").read_text()
        g._last_loaded = datetime.now(timezone.utc) - timedelta(hours=2)
        (tmp_path / "blocklist.json").unlink()

        response = httpx.Response(200, text=feed_text, request=httpx.Request("GET", "http://feed"))
        with (
            patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)),
            structlog.testing.capture_logs() as logs,
        ):
            await g._refresh()

        assert g.check_url("https://feed-evil.net/x").allowed is False
        assert _event(logs, "domain_guard_using_fallback")["source"] == "memory"
        assert not (tmp_path / "blocklist.json").exists(), "an empty feed must not write the cache"
        assert "feed-evil.net" in cache_before

    @pytest.mark.asyncio
    async def test_a_cache_timestamp_without_a_timezone_is_unreadable_not_a_crash(
        self, tmp_path: Path
    ) -> None:
        """A naive ``cached_at`` makes the cache unusable instead of raising TypeError."""
        cache_path = tmp_path / "blocklist.json"
        cache_path.write_text(
            json.dumps(
                {"cached_at": "2026-10-09T10:00:00", "domain_count": 1, "domains": ["x.net"]}
            )
        )
        g = DomainGuard(cache_path=cache_path, ttl_seconds=3600.0)

        with patch.object(g, "_fetch_urlhaus", new=AsyncMock(side_effect=ConnectionError("down"))):
            await g._refresh()

        assert g._blocklist == _BUNDLED_BLOCKLIST

    @pytest.mark.asyncio
    async def test_an_as_of_time_in_the_future_reports_an_age_of_zero(self, tmp_path: Path) -> None:
        """Clock skew never yields a negative age in the fallback log."""
        g = DomainGuard(cache_path=tmp_path / "blocklist.json", ttl_seconds=3600.0)
        with patch.object(g, "_fetch_urlhaus", new=AsyncMock(return_value={"feed-evil.net"})):
            await g._refresh()
        g._blocklist_as_of = datetime.now(timezone.utc) + timedelta(hours=3)
        (tmp_path / "blocklist.json").unlink()  # a fresh cache would answer before the fetch

        with (
            patch.object(g, "_fetch_urlhaus", new=AsyncMock(side_effect=ConnectionError("down"))),
            structlog.testing.capture_logs() as logs,
        ):
            await g._refresh()
        event = _event(logs, "domain_guard_using_fallback")
        assert event["source"] == "memory"
        assert event["age_seconds"] == 0.0
