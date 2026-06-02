"""Unit tests for the SLM-health process-global cache (FRE-399 / ADR-0083)."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest


def _make_snapshot() -> "SlmHealthSnapshot":
    from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot

    return SlmHealthSnapshot(
        status="up",
        reachable=True,
        probed_at=datetime.now(timezone.utc),
        trace_id="cache-test",
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure each test starts with a clean cache."""
    from personal_agent.observability.slm_health.cache import clear_cache

    clear_cache()
    yield
    clear_cache()


class TestGetCachedSnapshot:
    """get_cached_snapshot respects the TTL and returns None when empty."""

    def test_returns_none_when_empty(self) -> None:
        from personal_agent.observability.slm_health.cache import get_cached_snapshot

        assert get_cached_snapshot(ttl=60.0) is None

    def test_returns_snapshot_within_ttl(self) -> None:
        from personal_agent.observability.slm_health.cache import (
            get_cached_snapshot,
            set_cached_snapshot,
        )

        snap = _make_snapshot()
        set_cached_snapshot(snap)
        result = get_cached_snapshot(ttl=60.0)
        assert result is snap

    def test_returns_none_when_stale(self) -> None:
        """Snapshot stored in the past (simulated via monkeypatching _cached_at)."""
        import personal_agent.observability.slm_health.cache as cache_mod
        from personal_agent.observability.slm_health.cache import (
            get_cached_snapshot,
            set_cached_snapshot,
        )

        snap = _make_snapshot()
        set_cached_snapshot(snap)
        # Backdate the cached_at to simulate staleness
        cache_mod._cached_at = time.monotonic() - 100.0
        result = get_cached_snapshot(ttl=60.0)
        assert result is None

    def test_exact_ttl_boundary_returns_none(self) -> None:
        """At exactly TTL seconds old the snapshot is stale (> check)."""
        import personal_agent.observability.slm_health.cache as cache_mod
        from personal_agent.observability.slm_health.cache import (
            get_cached_snapshot,
            set_cached_snapshot,
        )

        snap = _make_snapshot()
        set_cached_snapshot(snap)
        cache_mod._cached_at = time.monotonic() - 45.0  # exactly TTL
        result = get_cached_snapshot(ttl=45.0)
        assert result is None


class TestSetCachedSnapshot:
    """set_cached_snapshot stores and overwrites."""

    def test_overwrites_previous(self) -> None:
        from personal_agent.observability.slm_health.cache import (
            get_cached_snapshot,
            set_cached_snapshot,
        )
        from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot

        snap_a = _make_snapshot()
        snap_b = SlmHealthSnapshot(
            status="degraded",
            reachable=True,
            probed_at=datetime.now(timezone.utc),
            trace_id="b",
        )
        set_cached_snapshot(snap_a)
        set_cached_snapshot(snap_b)
        result = get_cached_snapshot(ttl=60.0)
        assert result is snap_b
