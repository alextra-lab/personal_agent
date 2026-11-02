"""Security utilities for preventing information disclosure and egress URL guarding (FRE-225)."""

from __future__ import annotations

import asyncio
import csv
import functools
import io
import ipaddress
import json
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

import httpx

from personal_agent.config import settings
from personal_agent.config.env_loader import Environment
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    pass

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Domain Guard (FRE-225 / ADR-0028 egress security)
# ---------------------------------------------------------------------------

# Bundled fallback: a small set of well-known test/sinkholed malicious domains.
# Used when network feeds are unavailable on first start.
# Source: WiCAR test suite, Google Safe Browsing test hosts.
_BUNDLED_BLOCKLIST: frozenset[str] = frozenset(
    {
        "malware.wicar.org",
        "malware.testing.google.test",
        "testsafebrowsing.appspot.com",
        # FRE-1330: a model-emitted fetch_url call reached this exact bucket during a live
        # owner turn (trace 95df0b6bf51dc4c9f8a00712b8b865a5); not present anywhere in this
        # codebase's config or code, so the URL arrived as generated tokens rather than
        # something we constructed. Targeted, reversible block while the general novel-egress
        # signal (NovelDestinationTracker, below) lands as the durable mitigation.
        "routify-file-proxy-sg.oss-ap-southeast-1.aliyuncs.com",
    }
)

# FRE-1552: multi-tenant platforms that URLhaus lists by the malicious file, not by the host.
# Blocking such a hostname blocks every legitimate page on it (github.com, 2,516 feed URLs),
# so the guard blocks only the exact URLs the feed lists for these hosts. Any other host,
# including every bare IP, stays blocked as a whole. A shared host missing from this set
# is still blocked as a whole: add it here when a legitimate page on it is refused.
_SHARED_PLATFORM_HOSTS: frozenset[str] = frozenset(
    {
        "github.com",
        "raw.githubusercontent.com",
        "codeload.github.com",
        "gist.githubusercontent.com",
        "gitlab.com",
        "bitbucket.org",
        "drive.google.com",
        "docs.google.com",
        "firebasestorage.googleapis.com",
        "web.archive.org",
        "www.dropbox.com",
        "dl.dropboxusercontent.com",
        "cdn.discordapp.com",
        "media.discordapp.net",
        "res.cloudinary.com",
        "img1.wsimg.com",
        "files.pythonhosted.org",
    }
)

_PERCENT_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _canonical_escape(match: re.Match[str]) -> str:
    """Decode an escaped unreserved character; upper-case the hex of any other escape."""
    char = chr(int(match.group(1), 16))
    return char if char in _UNRESERVED else f"%{match.group(1).upper()}"


# URLhaus bulk export (FRE-1718). abuse.ch retired the keyless /downloads/ path (HTTP 410);
# the export now needs an Auth-Key as a URL path segment.
_URLHAUS_EXPORT_BASE = "https://urlhaus-api.abuse.ch/v2/files/exports"
# The one line that picks the export file (owner-provided 2026-11-01): the full JSON dump,
# a zip with one member. The parser also reads a plain-text URL list (one URL per line) or a
# CSV dump (URL in column 3, index 2), so another file is a one-line change.
_URLHAUS_EXPORT_FILE = "full.json.zip"
_URLHAUS_ARCHIVE_MEMBER = "urlhaus_full.json"
# The member is about 90 MB unpacked. A member over this limit is refused before it is read.
# The limit bounds the input only: the parsed JSON can use several times more memory than the
# bytes (about 2.5 times for a real dump). The feed comes from abuse.ch over TLS.
_MAX_UNPACKED_BYTES = 160 * 1024 * 1024
_ZIP_MAGIC = b"PK\x03\x04"
_URLHAUS_URL_COLUMN = 2
# A plain-text export line is a URL; a CSV row starts with a quote or a number.
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
# Fetch timeout for the feed refresh call (the export is about 32 MB).
_FEED_TIMEOUT_SECONDS = 60.0
# abuse.ch asks for at most one dump fetch per 5 minutes.
_MIN_FEED_FETCH_INTERVAL_SECONDS = 300.0
# A fallback list older than this raises ``domain_guard_blocklist_stale``.
_STALE_BLOCKLIST_ALERT_SECONDS = 86400.0

# Where the live blocklist came from (FRE-1560), and which list a failed refresh kept.
_BlocklistSource = Literal["bundled", "cache", "feed"]
_FallbackSource = Literal["memory", "stale_cache", "bundled"]


class UrlhausFeedError(RuntimeError):
    """The URLhaus download failed or was not attempted.

    The text never holds the Auth-Key: the key is part of the download URL, and an httpx
    error text includes the URL.
    """


class GuardMode(str, Enum):
    """Egress URL guard operating mode."""

    OFF = "off"
    BLOCKLIST = "blocklist"
    ALLOWLIST = "allowlist"


@dataclass(frozen=True)
class GuardResult:
    """Outcome of a DomainGuard.check_url() call.

    Attributes:
        allowed: True when the URL may be fetched.
        reason: Machine-readable reason string for telemetry.
        matched_entry: The blocklist/allowlist entry that triggered the decision,
            or None when no entry matched.
    """

    allowed: bool
    reason: str
    matched_entry: str | None = None


class DomainGuard:
    """Egress URL guard — checks outbound HTTP requests against a domain blocklist.

    Loads its blocklist from the URLhaus feed (CC0) and caches it to disk. Reloads
    automatically when the cache TTL expires. When a refresh fails it keeps the best
    list it has: the in-memory list, then the stale disk cache, then the bundled
    list (FRE-1560).

    The blocklist holds two kinds of entry (FRE-1552). A bare hostname blocks that
    host and its subdomains. A URL entry (``host/path?query``, no scheme) blocks
    only that exact resource and is used for hosts in ``_SHARED_PLATFORM_HOSTS``.

    Args:
        cache_path: JSON file used to persist the fetched blocklist.
        ttl_seconds: Cache lifetime; triggers a reload after this many seconds.
        mode: Operating mode (off / blocklist / allowlist).
        allowlist: Explicit allow-set used in ``GuardMode.ALLOWLIST`` mode.
            All other hostnames are blocked when this mode is active.
    """

    def __init__(
        self,
        cache_path: Path = Path("telemetry/security/domain_blocklist.json"),
        ttl_seconds: float = 3600.0,
        mode: GuardMode = GuardMode.BLOCKLIST,
        allowlist: frozenset[str] = frozenset(),
    ) -> None:
        """Initialise guard with cache location, TTL, and operating mode."""
        self._cache_path = cache_path
        self._ttl = ttl_seconds
        self._mode = mode
        self._allowlist = allowlist
        self._blocklist: frozenset[str] = _BUNDLED_BLOCKLIST
        self._blocklist_source: _BlocklistSource = "bundled"
        self._blocklist_as_of: datetime | None = None
        self._last_loaded: datetime | None = None
        self._refresh_lock: asyncio.Lock = asyncio.Lock()
        self._logged_stale: bool = False
        # Last feed request (FRE-1718). The marker file keeps it across a restart.
        self._last_fetch_attempt: datetime | None = None
        self._attempt_marker = cache_path.with_suffix(".attempt")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def mode(self) -> GuardMode:
        """Current operating mode — lets callers skip ``ensure_loaded()`` when off."""
        return self._mode

    async def ensure_loaded(self) -> None:
        """Load or refresh the blocklist if the cache is stale.

        Called by the brainstem scheduler's startup and periodic warm jobs
        (FRE-1162), not from the request path — ``_guard_request_hook`` calls
        ``note_staleness()`` instead, which never fetches. The lock still
        guards against overlapping scheduler ticks or an explicit
        ``refresh()`` racing this call.
        """
        if self._is_stale():
            async with self._refresh_lock:
                if self._is_stale():
                    await self._refresh()

    def note_staleness(self) -> None:
        """Cheap, synchronous freshness signal for the request-hot-path (FRE-1162).

        Never fetches — only ``ensure_loaded()``/``refresh()``, called from the
        brainstem scheduler's warm jobs, touch the network. Logs once per
        staleness episode so a stalled warm job is visible without the hot path
        paying for it or flooding logs on every request.
        """
        if self._mode is GuardMode.OFF or not self._is_stale():
            return
        if not self._logged_stale:
            log.warning(
                "domain_guard_stale_on_request_path",
                ttl_seconds=self._ttl,
                last_loaded=self._last_loaded.isoformat() if self._last_loaded else None,
            )
            self._logged_stale = True

    def check_url(self, url: str) -> GuardResult:
        """Check whether *url* is permitted under the current guard mode.

        Must be called after ``await guard.ensure_loaded()``.

        Args:
            url: Full URL (http/https) to evaluate.

        Returns:
            GuardResult with ``allowed`` flag, ``reason``, and matched entry.
        """
        if self._mode is GuardMode.OFF:
            return GuardResult(allowed=True, reason="guard_off")

        hostname = self._extract_hostname(url)
        if not hostname:
            return GuardResult(allowed=False, reason="invalid_hostname", matched_entry=url)

        if self._mode is GuardMode.ALLOWLIST:
            return self._check_allowlist(hostname)

        return self._check_blocklist(hostname, url)

    async def refresh(self) -> None:
        """Force a feed refresh regardless of TTL (e.g. from brainstem job)."""
        async with self._refresh_lock:
            await self._refresh()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_stale(self) -> bool:
        if self._last_loaded is None:
            return True
        elapsed = (datetime.now(timezone.utc) - self._last_loaded).total_seconds()
        return elapsed >= self._ttl

    @staticmethod
    def _extract_hostname(url: str) -> str:
        """Return the lowercased ASCII hostname from a URL, or '' on parse failure.

        Parsed with ``httpx.URL`` (FRE-1552), the parser that builds the request the hook
        sees: an IDN host is returned in its punycode form, a Unicode dot is mapped to
        ``.`` and a terminal dot is dropped. A feed line and a request therefore agree on one spelling.
        """
        try:
            host = httpx.URL(url).raw_host.decode("ascii")
        except httpx.InvalidURL, ValueError:
            return ""
        return host.lower().rstrip(".")

    def _domain_in_set(self, hostname: str, domain_set: frozenset[str]) -> str | None:
        """Return the matching entry if *hostname* or any parent domain is in *domain_set*."""
        parts = hostname.split(".")
        for i in range(len(parts)):
            candidate = ".".join(parts[i:])
            if candidate in domain_set:
                return candidate
        return None

    @staticmethod
    def _url_entry(url: str) -> str:
        """Return the scheme-less ``host/path?query`` form of *url* used for URL entries.

        The key is built from ``httpx.URL`` so a feed line and the request the hook later
        sees (already normalised by httpx: dot segments removed, unsafe characters
        percent-encoded) map to one entry. Spellings of the same octets also map to one
        entry: the case of a percent escape, an escaped unreserved character and an empty
        query. The scheme, port, userinfo and fragment are dropped. Anything else in the
        path or query is compared as written: extra query parameters or a reordered query
        is a different resource to this guard. Returns '' when *url* cannot be parsed.
        """
        host = DomainGuard._extract_hostname(url)
        if not host:
            return ""
        raw_path = httpx.URL(url).raw_path.decode("ascii").removesuffix("?")
        return f"{host}{_PERCENT_ESCAPE.sub(_canonical_escape, raw_path)}"

    def _check_blocklist(self, hostname: str, url: str) -> GuardResult:
        url_entry = self._url_entry(url)
        if url_entry in self._blocklist:
            return GuardResult(allowed=False, reason="blocklist_url_match", matched_entry=url_entry)
        matched = self._domain_in_set(hostname, self._blocklist)
        if matched:
            return GuardResult(allowed=False, reason="blocklist_match", matched_entry=matched)
        return GuardResult(allowed=True, reason="not_blocked")

    def _check_allowlist(self, hostname: str) -> GuardResult:
        matched = self._domain_in_set(hostname, self._allowlist)
        if matched:
            return GuardResult(allowed=True, reason="allowlist_match", matched_entry=matched)
        return GuardResult(allowed=False, reason="not_in_allowlist", matched_entry=hostname)

    def _mark_loaded(self) -> None:
        """Record a successful load and reset the staleness log throttle (FRE-1162)."""
        self._last_loaded = datetime.now(timezone.utc)
        self._logged_stale = False

    def _install(
        self, domains: frozenset[str], source: _BlocklistSource, as_of: datetime | None
    ) -> None:
        """Make *domains* the live blocklist, always unioned with ``_BUNDLED_BLOCKLIST``.

        Args:
            domains: Entries from the feed or the disk cache (empty for the bundled list).
            source: Where *domains* came from.
            as_of: When *domains* was produced: the fetch time, or the cache file's
                ``cached_at``. ``None`` for the bundled list.
        """
        self._blocklist = domains | _BUNDLED_BLOCKLIST
        self._blocklist_source = source
        self._blocklist_as_of = as_of
        self._mark_loaded()

    async def _refresh(self) -> None:
        """Reload blocklist: disk cache → URLhaus feed → best list still available."""
        cached = self._load_from_disk_cache()
        if cached is not None:
            # Union with _BUNDLED_BLOCKLIST (FRE-1330) — the disk cache holds only the last
            # URLhaus fetch's domains, so a cache written before a bundled entry was added
            # (e.g. this deploy's new targeted block) would otherwise silently drop that entry
            # for up to ttl_seconds, until the next network refresh. The fetch-feed branch
            # below already does this union; this branch must match it.
            cached_domains, cached_at = cached
            self._install(cached_domains, "cache", cached_at)
            log.debug("domain_guard_loaded_from_cache", count=len(self._blocklist))
            return

        try:
            fetched = await self._fetch_urlhaus()
            if not fetched:
                # A 200 response with no usable line must not replace a real list (FRE-1560).
                raise ValueError("feed returned no entries")
        except Exception as exc:
            log.warning("domain_guard_feed_unavailable", error=str(exc))
            self._fall_back()
            return

        self._install(frozenset(fetched), "feed", datetime.now(timezone.utc))
        # The fetched list is already live. A cache write error costs only the next cold
        # start, so it must not discard the list (FRE-1560).
        try:
            self._save_to_disk_cache(self._blocklist)
        except OSError as exc:
            log.warning(
                "domain_guard_cache_write_failed",
                error=str(exc),
                path=str(self._cache_path),
                count=len(self._blocklist),
            )
        log.info(
            "domain_guard_refreshed",
            source="urlhaus",
            export=_URLHAUS_EXPORT_FILE,
            count=len(self._blocklist),
        )

    def _fall_back(self) -> None:
        """Keep the best blocklist available after a failed feed fetch (FRE-1560).

        Order: the in-memory list when it came from the feed or the cache, then the disk
        cache with its TTL ignored, then the bundled list. Logs which source was used and,
        when the list is not the bundled one, its age.
        """
        now = datetime.now(timezone.utc)
        source: _FallbackSource
        if self._blocklist_source != "bundled" and self._blocklist_as_of is not None:
            source, as_of = "memory", self._blocklist_as_of
            self._mark_loaded()
        elif (stale := self._load_from_disk_cache(allow_stale=True)) is not None:
            domains, as_of = stale
            source = "stale_cache"
            self._install(domains, "cache", as_of)
        else:
            source, as_of = "bundled", None
            self._install(frozenset(), "bundled", None)
        age_seconds = max(0.0, (now - as_of).total_seconds()) if as_of else None
        log.warning(
            "domain_guard_using_fallback",
            source=source,
            count=len(self._blocklist),
            age_seconds=age_seconds,
        )
        # FRE-1718: a separate event for an alert rule. The bundled list (age None) is the
        # worst case: four entries.
        if age_seconds is None or age_seconds > _STALE_BLOCKLIST_ALERT_SECONDS:
            log.warning(
                "domain_guard_blocklist_stale",
                source=source,
                count=len(self._blocklist),
                age_seconds=age_seconds,
                max_age_seconds=_STALE_BLOCKLIST_ALERT_SECONDS,
            )

    def _load_from_disk_cache(
        self, *, allow_stale: bool = False
    ) -> tuple[frozenset[str], datetime] | None:
        """Return the cached entries and their ``cached_at`` if the cache file is usable.

        Args:
            allow_stale: When True, ignore the TTL (used after a failed feed fetch).

        Returns:
            ``(entries, cached_at)``, or None when the file is missing, unreadable, or
            older than the TTL and *allow_stale* is False.
        """
        if not self._cache_path.exists():
            return None
        try:
            data = json.loads(self._cache_path.read_text())
            cached_at = datetime.fromisoformat(data["cached_at"])
            if cached_at.tzinfo is None:
                return None  # this module writes aware timestamps; a naive one is not ours
            if (
                not allow_stale
                and (datetime.now(timezone.utc) - cached_at).total_seconds() >= self._ttl
            ):
                return None
            # A cache written before FRE-1552 names a whole platform by hostname. Drop only
            # those entries and keep the dedicated hosts: if the next feed fetch fails, the
            # guard must not fall back to the bundled list alone. The next refresh replaces
            # the dropped platforms with URL entries.
            return frozenset(data["domains"]) - _SHARED_PLATFORM_HOSTS, cached_at
        except json.JSONDecodeError, KeyError, ValueError, OSError, TypeError:
            # TypeError: the file parses but has the wrong shape (a list, a null field).
            return None

    def _save_to_disk_cache(self, domains: frozenset[str]) -> None:
        """Persist domains to the JSON cache file."""
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "domain_count": len(domains),
            "domains": sorted(domains),
        }
        self._cache_path.write_text(json.dumps(data, indent=2))

    def _seconds_since_fetch_attempt(self) -> float | None:
        """Return the seconds since the last feed request, or None when there was none.

        Reads the in-memory time and the marker file, so a restart does not reset it.
        """
        attempts: list[datetime] = []
        if self._last_fetch_attempt is not None:
            attempts.append(self._last_fetch_attempt)
        try:
            attempts.append(
                datetime.fromtimestamp(self._attempt_marker.stat().st_mtime, timezone.utc)
            )
        except OSError:
            pass  # no marker yet, or an unreadable one: only the in-memory time counts
        if not attempts:
            return None
        return max(0.0, (datetime.now(timezone.utc) - max(attempts)).total_seconds())

    def _note_fetch_attempt(self) -> None:
        """Record a feed request in memory and in the marker file (best effort)."""
        self._last_fetch_attempt = datetime.now(timezone.utc)
        try:
            self._attempt_marker.parent.mkdir(parents=True, exist_ok=True)
            self._attempt_marker.touch()
        except OSError as exc:
            log.warning(
                "domain_guard_attempt_marker_failed",
                error=str(exc),
                path=str(self._attempt_marker),
            )

    async def _fetch_urlhaus(self) -> set[str]:
        """Download the keyed URLhaus export and extract blocklist entries.

        Makes no request when the Auth-Key is unset, or when the last request was less than
        ``_MIN_FEED_FETCH_INTERVAL_SECONDS`` ago (abuse.ch limit). Every failure is raised as
        ``UrlhausFeedError`` with a text that has no URL, because the key is in the URL.

        Returns:
            One entry per export row: the hostname, or the URL entry when the host is a
            shared platform (see ``_SHARED_PLATFORM_HOSTS``).

        Raises:
            UrlhausFeedError: The key is unset, the fetch is throttled, or the request failed.
        """
        key = settings.abuse_ch_auth_key
        if not key:
            raise UrlhausFeedError("AGENT_ABUSE_CH_AUTH_KEY is not set")
        since = self._seconds_since_fetch_attempt()
        if since is not None and since < _MIN_FEED_FETCH_INTERVAL_SECONDS:
            raise UrlhausFeedError(
                f"fetch throttled: last request {since:.0f}s ago, "
                f"minimum {_MIN_FEED_FETCH_INTERVAL_SECONDS:.0f}s"
            )
        self._note_fetch_attempt()

        url = f"{_URLHAUS_EXPORT_BASE}/{quote(key, safe='')}/{_URLHAUS_EXPORT_FILE}"
        try:
            async with httpx.AsyncClient(timeout=_FEED_TIMEOUT_SECONDS) as client:
                resp = await client.get(url)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise UrlhausFeedError(f"feed returned HTTP {exc.response.status_code}") from None
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise UrlhausFeedError(f"feed request failed: {type(exc).__name__}") from None

        # About 160,000 records: parse off the event loop (the loop also serves the gateway).
        return await asyncio.to_thread(self._parse_urlhaus_export, resp.content)

    @staticmethod
    def _blocklist_entry(url: str) -> str | None:
        """Return the blocklist entry for a feed *url*, or None when it has no usable host.

        The entry is the hostname, or the URL entry when the host is a shared platform.
        """
        hostname = DomainGuard._extract_hostname(url)
        if not hostname:
            return None
        if hostname in _SHARED_PLATFORM_HOSTS:
            return DomainGuard._url_entry(url)
        return hostname

    @staticmethod
    def _parse_urlhaus_export(body: bytes) -> set[str]:
        """Extract blocklist entries from a URLhaus export.

        A body that starts with the zip signature is the full JSON dump. Any other body is
        text: a plain URL list (one URL per line) or a CSV dump (the URL in column
        ``_URLHAUS_URL_COLUMN``). A text line that starts with a URL scheme is a plain URL,
        even when the URL holds a comma. Lines that start with ``#`` are comments.

        Args:
            body: The export body.

        Returns:
            One entry per URL (see ``_blocklist_entry``).

        Raises:
            UrlhausFeedError: The archive is unreadable, too large, or not in the expected shape.
        """
        if body.startswith(_ZIP_MAGIC):
            urls = DomainGuard._urls_from_archive(body)
        else:
            urls = DomainGuard._urls_from_text(body.decode("utf-8", errors="replace"))
        return {entry for url in urls if (entry := DomainGuard._blocklist_entry(url))}

    @staticmethod
    def _urls_from_text(text: str) -> list[str]:
        """Return the URL of each data line of a plain-text or CSV export."""
        urls: list[str] = []
        for line in io.StringIO(text):
            if line.startswith("#"):
                continue
            if _URL_SCHEME.match(line):
                urls.append(line.strip())
                continue
            row = next(csv.reader([line]), [])
            if len(row) > _URLHAUS_URL_COLUMN:
                urls.append(row[_URLHAUS_URL_COLUMN].strip())
        return urls

    @staticmethod
    def _urls_from_archive(body: bytes) -> list[str]:
        """Return the URL of each record in the full JSON dump (a zip with one member).

        The JSON is a dict: a URLhaus id as the key and a list of records as the value.
        Each record dict is reduced to its URL while the JSON is parsed, so the 160,000
        records are never held in memory.

        Raises:
            UrlhausFeedError: The zip or the JSON is unreadable, the member is missing or
                larger than ``_MAX_UNPACKED_BYTES``, or the JSON has another shape.
        """
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                info = archive.getinfo(_URLHAUS_ARCHIVE_MEMBER)
                if info.file_size > _MAX_UNPACKED_BYTES:
                    raise UrlhausFeedError(
                        f"archive member is {info.file_size} bytes, over the limit "
                        f"of {_MAX_UNPACKED_BYTES}"
                    )
                # zipfile stops the read at the size in the header, so the limit holds.
                raw = archive.read(info)
        except UrlhausFeedError:
            raise
        except Exception as exc:  # BadZipFile, KeyError, zlib.error, RuntimeError (encrypted), ...
            raise UrlhausFeedError(f"archive unreadable: {type(exc).__name__}") from None

        def _record_url(record: dict[str, object]) -> object:
            url = record.get("url")
            return url if isinstance(url, str) else record

        try:
            parsed = json.loads(raw, object_hook=_record_url)
        except (
            ValueError,
            RecursionError,
        ) as exc:  # JSONDecodeError, UnicodeDecodeError, deep nesting
            raise UrlhausFeedError(f"archive JSON unreadable: {type(exc).__name__}") from None
        if not isinstance(parsed, dict):
            raise UrlhausFeedError("archive JSON is not a dict")
        return [
            item
            for records in parsed.values()
            if isinstance(records, list)
            for item in records
            if isinstance(item, str)
        ]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_guard: DomainGuard | None = None
_guard_lock = asyncio.Lock()


def get_domain_guard() -> DomainGuard:
    """Return the process-lifetime DomainGuard singleton (created on first call).

    Configuration is read from settings at creation time. The singleton is
    not thread-safe across process forks — create a new one in forked workers.
    """
    global _guard
    if _guard is None:
        _guard = DomainGuard(
            cache_path=Path("telemetry/security/domain_blocklist.json"),
            # Not below the abuse.ch fetch limit (FRE-1718), whatever the setting says.
            ttl_seconds=max(
                _MIN_FEED_FETCH_INTERVAL_SECONDS,
                float(getattr(settings, "url_guard_cache_ttl_seconds", 3600)),
            ),
            mode=GuardMode(getattr(settings, "url_guard_mode", "blocklist")),
            allowlist=frozenset(getattr(settings, "url_guard_allowlist", [])),
        )
    return _guard


# ---------------------------------------------------------------------------
# Novel egress destination signal (FRE-1330)
# ---------------------------------------------------------------------------

_NOVELTY_CACHE_PATH = Path("telemetry/security/egress_novelty_seen.json")


def _registrable_domain(hostname: str) -> str:
    """Approximate the eTLD+1 (registrable domain) of *hostname*.

    Uses the last two dot-separated labels, e.g. ``docs.exa.ai`` -> ``exa.ai``, so a
    subdomain of an already-seen site is not treated as a fresh novel destination.
    Correct for every domain in this deployment's actual fetch_url history (22 calls,
    all single-label public suffixes: .com, .org, .ai, ...). Known-wrong for a
    multi-part public suffix (e.g. ``a.co.uk`` collapses to ``co.uk`` instead of
    ``a.co.uk``) — stated as a residual gap rather than pulling in a public-suffix-list
    dependency for a case that has not occurred here.

    An IP literal (v4 or v6) is returned unchanged rather than collapsed by its
    trailing labels/groups — there is no eTLD+1 for an IP address, and collapsing one
    by its last two dot-separated parts would group unrelated hosts together (e.g.
    ``203.0.113.5`` and ``198.51.113.5`` would both become ``113.5``).
    """
    hostname = hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(hostname)
        return hostname
    except ValueError:
        pass
    parts = hostname.split(".")
    if len(parts) <= 2:
        return hostname
    return ".".join(parts[-2:])


@dataclass(frozen=True)
class NoveltyResult:
    """Outcome of a NovelDestinationTracker.check_and_record() call.

    Attributes:
        novel: True when this registrable domain was not seen within the tracker's
            window prior to this call.
        registrable_domain: The eTLD+1 the URL was recorded under.
    """

    novel: bool
    registrable_domain: str


class NovelDestinationTracker:
    """Tracks registrable domains fetch_url has targeted; flags first-ever ones.

    Observe-only (FRE-1330): never blocks, only distinguishes "seen within the prior
    window" from "novel" so a caller can log a distinct event for the latter. Persists
    sightings to disk (survives restarts), keyed on eTLD+1 rather than full hostname.

    The `asyncio.Lock` guarding the cache transaction is process-local — same scope as
    `DomainGuard`'s own `_refresh_lock` — so a multi-process deployment would need
    cross-process coordination this does not attempt, matching that existing gap
    rather than introducing a new one.

    Args:
        cache_path: JSON file used to persist domain -> last-seen-ISO-timestamp.
        window_seconds: How long a sighting counts as "not novel".
    """

    def __init__(
        self,
        cache_path: Path = _NOVELTY_CACHE_PATH,
        window_seconds: float = 14 * 86400.0,
    ) -> None:
        """Initialise the tracker with its cache location and novelty window."""
        self._cache_path = cache_path
        self._window = window_seconds
        self._lock: asyncio.Lock = asyncio.Lock()

    async def check_and_record(self, url: str) -> NoveltyResult:
        """Return whether url's registrable domain is novel, and record the sighting.

        The load, novelty check, sighting update, prune, and save all happen inside
        one lock acquisition, so concurrent calls for the same brand-new domain cannot
        both observe "novel" — exactly one does, and the rest see the just-recorded
        sighting.

        Args:
            url: The URL fetch_url was asked to target.

        Returns:
            NoveltyResult with the novelty verdict and the registrable domain used.
        """
        hostname = DomainGuard._extract_hostname(url)
        if not hostname:
            return NoveltyResult(novel=False, registrable_domain="")
        domain = _registrable_domain(hostname)

        async with self._lock:
            seen = self._load()
            now = datetime.now(timezone.utc)
            novel = domain not in seen or self._is_expired(seen[domain], now)
            seen[domain] = now.isoformat()
            seen = {d: ts for d, ts in seen.items() if not self._is_expired(ts, now)}
            self._save(seen)

        return NoveltyResult(novel=novel, registrable_domain=domain)

    def _is_expired(self, last_seen_iso: str, now: datetime) -> bool:
        try:
            last_seen = datetime.fromisoformat(last_seen_iso)
        except ValueError, TypeError:
            return True
        return (now - last_seen).total_seconds() > self._window

    def _load(self) -> dict[str, str]:
        if not self._cache_path.exists():
            return {}
        try:
            data = json.loads(self._cache_path.read_text())
            if not isinstance(data, dict):
                return {}
            return data
        except json.JSONDecodeError, OSError:
            return {}

    def _save(self, seen: dict[str, str]) -> None:
        """Persist sightings via a temp-file-then-replace.

        Avoids leaving truncated JSON if the process crashes mid-write. Failures are
        logged and swallowed — this is an observe-only signal and must never
        propagate into the fetch_url call path.
        """
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(seen, indent=2))
            tmp_path.replace(self._cache_path)
        except OSError as exc:
            log.warning("egress_novelty_save_failed", error=str(exc))


_novelty_tracker: NovelDestinationTracker | None = None


def get_novelty_tracker() -> NovelDestinationTracker:
    """Return the process-lifetime NovelDestinationTracker singleton (created lazily).

    Window is read from settings at creation time.
    """
    global _novelty_tracker
    if _novelty_tracker is None:
        _novelty_tracker = NovelDestinationTracker(
            cache_path=_NOVELTY_CACHE_PATH,
            window_seconds=float(getattr(settings, "url_guard_novelty_window_days", 14)) * 86400,
        )
    return _novelty_tracker


# ---------------------------------------------------------------------------
# Outbound transport factory (ADR-0132 D2 / FRE-1147)
# ---------------------------------------------------------------------------


class EgressBlockedError(httpx.RequestError):
    """Raised by the DomainGuard request hook before a request is ever sent.

    Subclasses ``httpx.RequestError`` (not ``httpx.TransportError`` — this
    never reaches a transport) so it is still caught by any existing
    ``except httpx.RequestError`` / ``except httpx.HTTPError`` handler at a
    seam call site. A seam whose except clauses are narrower (e.g. only
    ``httpx.ConnectError``) needs an explicit ``except EgressBlockedError``.
    """

    def __init__(self, request: httpx.Request, reason: str, matched_entry: str | None) -> None:
        """Build the error from the refused request and the guard's verdict.

        Args:
            request: The httpx request that was refused.
            reason: Machine-readable reason from ``GuardResult.reason``.
            matched_entry: The blocklist/allowlist entry that triggered the
                refusal, if any.
        """
        super().__init__(
            f"egress blocked by DomainGuard: {request.url} ({reason})",
            request=request,
        )
        self.reason = reason
        self.matched_entry = matched_entry


async def _guard_request_hook(request: httpx.Request, *, guard: DomainGuard) -> None:
    """Httpx async request hook — refuses a request before it is sent.

    Never fetches (FRE-1162): the blocklist is warmed by the brainstem
    scheduler's startup and periodic jobs, not on the request path. This only
    logs a cheap staleness signal (``note_staleness()``) and reads whatever
    blocklist is already in memory.
    """
    guard.note_staleness()
    result = guard.check_url(str(request.url))
    if not result.allowed:
        log.warning(
            "egress_blocked",
            url=str(request.url),
            reason=result.reason,
            matched_entry=result.matched_entry,
        )
        raise EgressBlockedError(request, result.reason, result.matched_entry)


def check_egress_or_raise(
    url: str, *, guard: DomainGuard | None = None, trace_id: str | None = None
) -> None:
    """Layer-1 pre-dispatch DomainGuard check (ADR-0141 D2.1).

    Route-independent: callers invoke this before handing dispatch to litellm,
    so the caller-facing exception type never depends on unwrapping a
    provider route's own wrapper shape (some routes raise without
    ``from e``, making the cause chain unreliable). Mirrors
    ``_guard_request_hook``'s never-fetches behaviour (FRE-1162).

    Args:
        url: The resolved endpoint/api_base about to be dispatched to.
        guard: DomainGuard instance to consult; defaults to the process
            singleton (``get_domain_guard()``).
        trace_id: Caller's trace id, when in scope, so an egress-block event
            on the LLM dispatch path can be correlated to the turn that
            triggered it. ``_guard_request_hook`` (the httpx event-hook this
            mirrors) has no request-scoped trace context available to it and
            omits it for the same reason; this seam does have one available.

    Raises:
        EgressBlockedError: If the guard refuses ``url``.
    """
    resolved_guard = guard or get_domain_guard()
    resolved_guard.note_staleness()
    result = resolved_guard.check_url(url)
    if not result.allowed:
        log.warning(
            "egress_blocked_pre_dispatch",
            url=url,
            reason=result.reason,
            matched_entry=result.matched_entry,
            trace_id=trace_id,
        )
        raise EgressBlockedError(httpx.Request("POST", url), result.reason, result.matched_entry)


def guard_event_hooks(
    *, guard: DomainGuard | None = None
) -> dict[str, list[Callable[..., object]]]:
    """Build an ``event_hooks`` dict carrying the DomainGuard's request hook.

    For httpx-compatible objects that accept ``event_hooks=`` directly at
    construction (e.g. litellm's ``AsyncHTTPHandler``, whose own
    ``event_hooks`` parameter is typed ``Mapping[str, list[Callable[...,
    object]]]``) rather than being built via
    :func:`create_guarded_http_client`. Same hook, same guard behaviour,
    different injection point (ADR-0141 D2.1 layer 2).

    Args:
        guard: DomainGuard instance to consult; defaults to the process
            singleton (``get_domain_guard()``).

    Returns:
        An ``event_hooks`` dict with the guard hook as the sole ``request``
        entry.
    """
    resolved_guard = guard or get_domain_guard()
    return {"request": [functools.partial(_guard_request_hook, guard=resolved_guard)]}


def create_guarded_http_client(
    *,
    guard: DomainGuard | None = None,
    **client_kwargs: Any,
) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` whose every request is checked by DomainGuard first.

    This is the one outbound transport factory obliged by ADR-0132 D2: every
    enumerated egress seam constructs its client through this function
    instead of constructing ``httpx.AsyncClient`` directly — or passes the
    client it returns as ``http_client=`` to an Anthropic/OpenAI SDK client.

    Every ``httpx.AsyncClient`` keyword continues to work unchanged (verify,
    cert, timeout, follow_redirects, proxy, mounts, transport, ...) — this
    only installs the DomainGuard check as the first request hook, ahead of
    any caller-supplied hooks, so it fires before transport/mount/proxy
    selection and on every redirect (``AsyncClient._send_handling_redirects``
    runs request hooks strictly before transport dispatch, httpx 0.28).

    Args:
        guard: DomainGuard instance to consult; defaults to the process
            singleton (``get_domain_guard()``).
        **client_kwargs: Forwarded to ``httpx.AsyncClient`` unmodified,
            except ``event_hooks`` which is merged with the guard hook.

    Returns:
        An ``httpx.AsyncClient`` that refuses disallowed URLs pre-connection.
    """
    resolved_guard = guard or get_domain_guard()
    existing_hooks = dict(client_kwargs.pop("event_hooks", None) or {})
    request_hooks = [
        functools.partial(_guard_request_hook, guard=resolved_guard),
        *existing_hooks.get("request", []),
    ]
    return httpx.AsyncClient(
        event_hooks={**existing_hooks, "request": request_hooks},
        **client_kwargs,
    )


def _user_message_with_debug_hint(base: str, error_type: str, error_str: str) -> str:
    """Append a safe debug hint in development/debug so the real error is visible."""
    if not settings.debug and settings.environment != Environment.DEVELOPMENT:
        return base
    snippet = (error_str or "").strip()[:200]
    if snippet:
        return f"{base} (Debug: {error_type}: {snippet})"
    return f"{base} (Debug: {error_type})"


def sanitize_error_message(error: Exception) -> str:
    """Create a user-friendly error message without exposing sensitive details.

    This function filters out sensitive information like file paths, stack traces,
    memory addresses, and other internal details that could leak system information.
    In development or when debug is True, appends a safe hint with error type and
    a redacted snippet so the underlying cause can be diagnosed.

    Args:
        error: The exception that occurred

    Returns:
        A sanitized, user-friendly error message
    """
    error_type = type(error).__name__
    error_str = str(error)

    # Filter out sensitive patterns (file paths, stack traces, etc.)
    # Remove absolute paths
    error_str = re.sub(r"/[^\s]+", "[path]", error_str)
    # Remove common sensitive patterns
    error_str = re.sub(r"0x[0-9a-fA-F]+", "[address]", error_str)
    error_str = re.sub(r"line \d+", "[line]", error_str)

    # Categorize errors and provide helpful messages
    if "Connection" in error_type or "connection" in error_str.lower():
        return _user_message_with_debug_hint(
            "Unable to connect to the language model service. Please try again in a moment.",
            error_type,
            error_str,
        )
    elif "Timeout" in error_type or "timeout" in error_str.lower():
        return _user_message_with_debug_hint(
            "The request took too long to process. Please try again with a simpler request.",
            error_type,
            error_str,
        )
    elif "Permission" in error_type or "permission" in error_str.lower():
        return "Permission denied. Please check your configuration."
    elif "Validation" in error_type or "validation" in error_str.lower():
        return _user_message_with_debug_hint(
            "Invalid request format. Please check your input and try again.",
            error_type,
            error_str,
        )
    elif "NotFound" in error_type or "not found" in error_str.lower():
        return "The requested resource was not found."
    elif "RateLimit" in error_type or "rate limit" in error_str.lower():
        return "Too many requests. Please wait a moment and try again."
    elif "Configuration" in error_type or "config" in error_str.lower():
        return "Service configuration error. Please contact support."
    else:
        return _user_message_with_debug_hint(
            "An error occurred while processing your request. Please try again.",
            error_type,
            error_str,
        )
