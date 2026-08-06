"""Unit tests for the outbound transport factory — ADR-0132 D2 / FRE-1147.

All tests run without network access. The factory's contract:
1. `create_guarded_http_client` installs a DomainGuard check as the first
   request hook, so a disallowed URL raises `EgressBlockedError` before any
   connection is attempted.
2. The hook never touches `ensure_loaded()` in ANY mode (FRE-1162) — the
   blocklist is warmed by the brainstem scheduler, not the request path;
   the hook only calls the non-fetching `note_staleness()`.
3. `GuardMode.BLOCKLIST` (the settings.py default) passes through unaffected
   when nothing matches, and still refuses a matching domain even when the
   cache is stale — the fetch is skipped, not the blocking decision.
4. Caller-supplied kwargs (timeout, follow_redirects, headers, verify, ...)
   and caller-supplied event hooks are preserved untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from personal_agent.security import (
    DomainGuard,
    EgressBlockedError,
    GuardMode,
    create_guarded_http_client,
)


def _guard(
    tmp_path: Path,
    *,
    mode: GuardMode,
    blocklist: frozenset[str] = frozenset(),
    allowlist: frozenset[str] = frozenset(),
) -> DomainGuard:
    """A pre-loaded DomainGuard — never touches network or disk."""
    g = DomainGuard(
        cache_path=tmp_path / "blocklist.json",
        mode=mode,
        allowlist=allowlist,
    )
    g._blocklist = blocklist
    g._last_loaded = datetime.now(timezone.utc)
    return g


def _ok_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


class TestAllowlistRefusal:
    @pytest.mark.asyncio
    async def test_disallowed_host_refused_before_connection(self, tmp_path: Path) -> None:
        guard = _guard(tmp_path, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"}))
        client = create_guarded_http_client(guard=guard, transport=_ok_transport())
        async with client:
            with pytest.raises(EgressBlockedError):
                await client.get("https://not-allowed.example/path")

    @pytest.mark.asyncio
    async def test_allowed_host_reaches_transport(self, tmp_path: Path) -> None:
        guard = _guard(tmp_path, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"}))
        client = create_guarded_http_client(guard=guard, transport=_ok_transport())
        async with client:
            resp = await client.get("https://allowed.example/path")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_blocked_error_carries_reason(self, tmp_path: Path) -> None:
        guard = _guard(tmp_path, mode=GuardMode.ALLOWLIST, allowlist=frozenset())
        client = create_guarded_http_client(guard=guard, transport=_ok_transport())
        async with client:
            with pytest.raises(EgressBlockedError) as exc_info:
                await client.get("https://anything.example/path")
        assert exc_info.value.reason == "not_in_allowlist"


class TestOffModeNoLatencyRegression:
    @pytest.mark.asyncio
    async def test_off_mode_never_calls_ensure_loaded(self, tmp_path: Path) -> None:
        guard = DomainGuard(cache_path=tmp_path / "blocklist.json", mode=GuardMode.OFF)
        guard.ensure_loaded = AsyncMock(wraps=guard.ensure_loaded)  # type: ignore[method-assign]
        client = create_guarded_http_client(guard=guard, transport=_ok_transport())
        async with client:
            resp = await client.get("https://anything.example/path")
        assert resp.status_code == 200
        guard.ensure_loaded.assert_not_awaited()


class TestRequestPathNeverFetches:
    """FRE-1162: the hook must never touch ensure_loaded() in ANY mode, not just OFF."""

    @pytest.mark.asyncio
    async def test_blocklist_mode_never_calls_ensure_loaded(self, tmp_path: Path) -> None:
        """A stale BLOCKLIST guard — the mode that pays the fetch today — must not fetch.

        Uses a plain AsyncMock (not ``wraps=guard.ensure_loaded``) so a regression that
        actually calls it fails the assertion instead of silently performing a real fetch
        through the wrapped mock.
        """
        guard = DomainGuard(cache_path=tmp_path / "blocklist.json", mode=GuardMode.BLOCKLIST)
        guard._blocklist = frozenset()
        guard._last_loaded = None  # never loaded => stale
        guard.ensure_loaded = AsyncMock()  # type: ignore[method-assign]
        client = create_guarded_http_client(guard=guard, transport=_ok_transport())
        async with client:
            resp = await client.get("https://anything.example/path")
        assert resp.status_code == 200
        guard.ensure_loaded.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hook_calls_note_staleness(self, tmp_path: Path) -> None:
        """The hook still performs a freshness signal — it doesn't just drop the check."""
        guard = DomainGuard(cache_path=tmp_path / "blocklist.json", mode=GuardMode.BLOCKLIST)
        guard._blocklist = frozenset()
        guard.note_staleness = MagicMock(wraps=guard.note_staleness)  # type: ignore[method-assign]
        client = create_guarded_http_client(guard=guard, transport=_ok_transport())
        async with client:
            await client.get("https://anything.example/path")
        guard.note_staleness.assert_called_once()

    @pytest.mark.asyncio
    async def test_stale_blocklist_still_refuses_without_fetching(self, tmp_path: Path) -> None:
        """Staleness affects only the fetch, never the blocking decision itself."""
        guard = DomainGuard(cache_path=tmp_path / "blocklist.json", mode=GuardMode.BLOCKLIST)
        guard._blocklist = frozenset({"evil.example"})
        guard._last_loaded = None  # stale
        guard.ensure_loaded = AsyncMock()  # type: ignore[method-assign]
        client = create_guarded_http_client(guard=guard, transport=_ok_transport())
        async with client:
            with pytest.raises(EgressBlockedError):
                await client.get("https://evil.example/path")
        guard.ensure_loaded.assert_not_awaited()


class TestBlocklistModeDefaultNoRegression:
    @pytest.mark.asyncio
    async def test_blocklist_mode_passes_non_matching_hosts(self, tmp_path: Path) -> None:
        guard = _guard(tmp_path, mode=GuardMode.BLOCKLIST, blocklist=frozenset({"evil.example"}))
        client = create_guarded_http_client(guard=guard, transport=_ok_transport())
        async with client:
            resp = await client.get("https://slm.internal.example/v1/chat")
        assert resp.status_code == 200


class TestKwargPassthrough:
    @pytest.mark.asyncio
    async def test_client_kwargs_pass_through_unmodified(self, tmp_path: Path) -> None:
        guard = _guard(tmp_path, mode=GuardMode.OFF)
        client = create_guarded_http_client(
            guard=guard,
            transport=_ok_transport(),
            timeout=httpx.Timeout(5.0),
            follow_redirects=True,
            headers={"X-Test": "1"},
        )
        assert client.follow_redirects is True
        assert client.headers["X-Test"] == "1"

    @pytest.mark.asyncio
    async def test_caller_supplied_event_hooks_preserved(self, tmp_path: Path) -> None:
        guard = _guard(tmp_path, mode=GuardMode.OFF)
        seen: list[httpx.Request] = []

        async def caller_hook(request: httpx.Request) -> None:
            seen.append(request)

        client = create_guarded_http_client(
            guard=guard,
            transport=_ok_transport(),
            event_hooks={"request": [caller_hook]},
        )
        async with client:
            await client.get("https://anything.example/path")
        assert len(seen) == 1
