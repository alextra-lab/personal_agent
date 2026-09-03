"""Security utilities for preventing information disclosure and egress URL guarding (FRE-225)."""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

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
        self._logged_stale: bool = False

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

    def _mark_loaded(self) -> None:
        """Record a successful load and reset the staleness log throttle (FRE-1162)."""
        self._last_loaded = datetime.now(timezone.utc)
        self._logged_stale = False

    async def _refresh(self) -> None:
        """Reload blocklist: disk cache → URLhaus feed → bundled fallback."""
        cached = self._load_from_disk_cache()
        if cached is not None:
            # Union with _BUNDLED_BLOCKLIST (FRE-1330) — the disk cache holds only the last
            # URLhaus fetch's domains, so a cache written before a bundled entry was added
            # (e.g. this deploy's new targeted block) would otherwise silently drop that entry
            # for up to ttl_seconds, until the next network refresh. The fetch-feed branch
            # below already does this union; this branch must match it.
            self._blocklist = cached | _BUNDLED_BLOCKLIST
            self._mark_loaded()
            log.debug("domain_guard_loaded_from_cache", count=len(self._blocklist))
            return

        try:
            domains = await self._fetch_urlhaus()
            self._blocklist = frozenset(domains) | _BUNDLED_BLOCKLIST
            self._mark_loaded()
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
            self._mark_loaded()
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
        except (ValueError, TypeError):
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
        except (json.JSONDecodeError, OSError):
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
