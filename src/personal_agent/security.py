"""Security utilities for preventing information disclosure and egress URL guarding (FRE-225)."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

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
    }
)

# URLhaus plaintext feed URL (CC0 licence, no key required).
_URLHAUS_FEED = "https://urlhaus.abuse.ch/downloads/text/"
# Fetch timeout for the feed refresh call.
_FEED_TIMEOUT_SECONDS = 15.0


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

    Loads its blocklist from the URLhaus feed (CC0) and caches it to disk.
    Falls back to a bundled list when the network is unavailable. Reloads
    automatically when the cache TTL expires.

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
        self._last_loaded: datetime | None = None
        self._refresh_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ensure_loaded(self) -> None:
        """Load or refresh the blocklist if the cache is stale.

        Safe to call from every request — the lock prevents duplicate refreshes.
        """
        if self._is_stale():
            async with self._refresh_lock:
                if self._is_stale():
                    await self._refresh()

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

        return self._check_blocklist(hostname)

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
        """Return the lowercased hostname from a URL, or '' on parse failure."""
        try:
            h = urlparse(url).hostname
            return h.lower() if h else ""
        except Exception:
            return ""

    def _domain_in_set(self, hostname: str, domain_set: frozenset[str]) -> str | None:
        """Return the matching entry if *hostname* or any parent domain is in *domain_set*."""
        parts = hostname.split(".")
        for i in range(len(parts)):
            candidate = ".".join(parts[i:])
            if candidate in domain_set:
                return candidate
        return None

    def _check_blocklist(self, hostname: str) -> GuardResult:
        matched = self._domain_in_set(hostname, self._blocklist)
        if matched:
            return GuardResult(allowed=False, reason="blocklist_match", matched_entry=matched)
        return GuardResult(allowed=True, reason="not_blocked")

    def _check_allowlist(self, hostname: str) -> GuardResult:
        matched = self._domain_in_set(hostname, self._allowlist)
        if matched:
            return GuardResult(allowed=True, reason="allowlist_match", matched_entry=matched)
        return GuardResult(allowed=False, reason="not_in_allowlist", matched_entry=hostname)

    async def _refresh(self) -> None:
        """Reload blocklist: disk cache → URLhaus feed → bundled fallback."""
        cached = self._load_from_disk_cache()
        if cached is not None:
            self._blocklist = cached
            self._last_loaded = datetime.now(timezone.utc)
            log.debug("domain_guard_loaded_from_cache", count=len(self._blocklist))
            return

        try:
            domains = await self._fetch_urlhaus()
            self._blocklist = frozenset(domains) | _BUNDLED_BLOCKLIST
            self._last_loaded = datetime.now(timezone.utc)
            self._save_to_disk_cache(self._blocklist)
            log.info(
                "domain_guard_refreshed",
                source="urlhaus",
                count=len(self._blocklist),
            )
        except Exception as exc:
            log.warning(
                "domain_guard_feed_unavailable",
                error=str(exc),
                fallback_count=len(_BUNDLED_BLOCKLIST),
            )
            self._blocklist = _BUNDLED_BLOCKLIST
            self._last_loaded = datetime.now(timezone.utc)
            log.warning(
                "domain_guard_using_bundled_fallback",
                count=len(_BUNDLED_BLOCKLIST),
            )

    def _load_from_disk_cache(self) -> frozenset[str] | None:
        """Return cached domains if the cache file exists and is within TTL."""
        if not self._cache_path.exists():
            return None
        try:
            data = json.loads(self._cache_path.read_text())
            cached_at = datetime.fromisoformat(data["cached_at"])
            if (datetime.now(timezone.utc) - cached_at).total_seconds() >= self._ttl:
                return None
            return frozenset(data["domains"])
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
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

    async def _fetch_urlhaus(self) -> set[str]:
        """Download the URLhaus plaintext feed and extract unique hostnames."""
        import httpx

        async with httpx.AsyncClient(timeout=_FEED_TIMEOUT_SECONDS) as client:
            resp = await client.get(_URLHAUS_FEED)
            resp.raise_for_status()

        domains: set[str] = set()
        for line in resp.text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            hostname = self._extract_hostname(line)
            if hostname:
                domains.add(hostname)
        return domains


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
            ttl_seconds=float(getattr(settings, "url_guard_cache_ttl_seconds", 3600)),
            mode=GuardMode(getattr(settings, "url_guard_mode", "blocklist")),
            allowlist=frozenset(getattr(settings, "url_guard_allowlist", [])),
        )
    return _guard


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
