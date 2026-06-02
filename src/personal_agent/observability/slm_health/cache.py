"""Process-global SLM-health snapshot cache (FRE-399 / ADR-0083).

Stores the most recent :class:`~.snapshot.SlmHealthSnapshot` in module-level
state so the ``/api/inference/status`` endpoint and the executor error-reason
hint can read the last known SLM health **without** making a new network call.

This is intentionally minimal: one value, one timestamp, one TTL check.  No
asyncio locks are used — a stale read or a double-write both have bounded,
harmless consequences (worst case: a slightly-old hint on the first probe after
a restart).

Thread-safety note: CPython's GIL makes simple assignment atomic, so the
module-level slots are safe without an explicit lock on CPython. On alternative
runtimes, wrap with ``asyncio.Lock`` if needed.
"""

from __future__ import annotations

import time

from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot

_cached_snapshot: SlmHealthSnapshot | None = None
_cached_at: float = 0.0  # monotonic timestamp


def set_cached_snapshot(snapshot: SlmHealthSnapshot) -> None:
    """Store *snapshot* as the latest SLM health reading.

    Args:
        snapshot: Freshly probed :class:`~.snapshot.SlmHealthSnapshot`.
    """
    global _cached_snapshot, _cached_at
    _cached_snapshot = snapshot
    _cached_at = time.monotonic()


def get_cached_snapshot(ttl: float) -> SlmHealthSnapshot | None:
    """Return the cached snapshot if it is still fresh, else ``None``.

    Args:
        ttl: Maximum age in seconds before the cached value is considered stale.
            Matches ``settings.slm_health_cache_ttl_seconds``.

    Returns:
        The cached :class:`~.snapshot.SlmHealthSnapshot`, or ``None`` when no
        snapshot has been stored yet or the stored value is older than *ttl*.
    """
    if _cached_snapshot is None:
        return None
    if time.monotonic() - _cached_at > ttl:
        return None
    return _cached_snapshot


def clear_cache() -> None:
    """Reset the module-level cache (test helper).

    Only intended for use in unit tests to ensure probe tests start from a
    clean state. Do not call from production code.
    """
    global _cached_snapshot, _cached_at
    _cached_snapshot = None
    _cached_at = 0.0
