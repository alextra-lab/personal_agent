"""Process-global SLM-health verdict — the state model (FRE-399 / ADR-0083, FRE-1435 / ADR-0144 D4).

Stores the latest :class:`~.snapshot.SlmHealthSnapshot` in module-level state so readers
(``provider_health.is_provider_available``, the executor error-reason hint) get the last known
SLM health **without** making a new network call.

Two writers publish here: the scheduled probe, and a real local call (FRE-1436). Four rules
govern the one slot:

1. **Progress outranks silence.** :func:`inflight_verdict` reads the in-flight registry: one
   call that advanced its last-byte stamp within the threshold means the box is generating.
2. **The newer observation wins.** A write lands only when its ``observed_monotonic`` is newer
   than the stored one. A slow probe that started before a call failed cannot overwrite that
   call's fresher ``down``.
3. **``down`` is sticky.** A ``down`` does not expire. Only a newer observation that the
   origin generated replaces it (:func:`_clears_down`). ``up`` and ``degraded`` expire at the
   TTL, measured from observation.
4. **A ``down`` shortens the probe interval** — applied by the scheduler, see
   :func:`~.scheduler_runner.slm_health_probe_interval_s`.

No lock is used. The slot is read and written on the one event loop of the one worker, with no
``await`` between the compare and the store (the ADR-0143 D1 invariant).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.llm_client.inflight import InflightCall

log = get_logger(__name__)

_cached_snapshot: SlmHealthSnapshot | None = None


def set_cached_snapshot(snapshot: SlmHealthSnapshot) -> bool:
    """Store *snapshot* as the verdict if its evidence is newer than the stored one.

    Args:
        snapshot: The new :class:`~.snapshot.SlmHealthSnapshot`.

    Returns:
        ``True`` when the write landed. ``False`` when the stored verdict was observed at the
        same time or later, which leaves the stored verdict in place (rule 2).
    """
    global _cached_snapshot
    stored = _cached_snapshot
    reason: str | None = None
    if stored is not None and snapshot.observed_monotonic <= stored.observed_monotonic:
        reason = "not_newer"
    elif stored is not None and stored.status == "down" and not _clears_down(snapshot, stored):
        reason = "no_generation_evidence"
    if stored is not None and reason is not None:
        log.info(
            "slm_health_write_rejected",
            reason=reason,
            trace_id=snapshot.trace_id,
            writer=snapshot.writer,
            status=snapshot.status,
            stored_trace_id=stored.trace_id,
            stored_writer=stored.writer,
            stored_status=stored.status,
            observed_lag_s=round(stored.observed_monotonic - snapshot.observed_monotonic, 3),
            component="slm_health",
        )
        return False
    _cached_snapshot = snapshot
    return True


def _clears_down(snapshot: SlmHealthSnapshot, stored_down: SlmHealthSnapshot) -> bool:
    """Return whether *snapshot* is positive evidence that clears *stored_down* (rule 3).

    A ``down`` clears only on evidence that the origin generated something: a real local call
    (a call writes a non-``down`` verdict only when it completed), or a probe whose generation
    check succeeded. A liveness-only probe (``generation_ok is None``) clears only a ``down``
    that a probe wrote, because that ``down`` itself rested on liveness alone. It never clears
    a call-written ``down``: a ``/health`` that answers says nothing about a wedged generation
    slot (FRE-1436's nine ``up`` events).

    Args:
        snapshot: The newer verdict.
        stored_down: The stored ``down``.

    Returns:
        ``True`` when *snapshot* may replace *stored_down*.
    """
    if snapshot.status == "down":
        return True
    if snapshot.writer == "call" or snapshot.generation_ok is True:
        return True
    if snapshot.generation_ok is False:
        return False
    return stored_down.writer == "probe"


def get_cached_snapshot(ttl: float) -> SlmHealthSnapshot | None:
    """Return the stored verdict if it still stands, else ``None``.

    Args:
        ttl: Seconds an ``up`` or ``degraded`` verdict stays fresh, measured from its
            observation. Matches ``settings.slm_health_cache_ttl_seconds``.

    Returns:
        The stored ``down`` at any age (rule 3). The stored ``up`` or ``degraded`` while it is
        within *ttl*. ``None`` when nothing is stored, or the stored verdict expired.
    """
    stored = _cached_snapshot
    if stored is None:
        return None
    if stored.status == "down":
        return stored
    if time.monotonic() - stored.observed_monotonic > ttl:
        return None
    return stored


def verdict_is_down() -> bool:
    """Return whether the stored verdict is an uncleared ``down``.

    Returns:
        ``True`` only when a ``down`` is stored. An expired ``up``/``degraded`` and an empty
        slot (the cold start) both return ``False``.
    """
    stored = _cached_snapshot
    return stored is not None and stored.status == "down"


def inflight_verdict(
    calls: Sequence[InflightCall],
    *,
    silence_threshold_s: float,
    now: float | None = None,
) -> Literal["up", "down"] | None:
    """Return the verdict that the in-flight local calls support (rule 1).

    Args:
        calls: The live attempts, from
            :meth:`~personal_agent.llm_client.inflight.InflightCallRegistry.snapshot`.
        silence_threshold_s: Seconds of silence past which one call counts as stalled.
        now: A ``time.monotonic()`` reading. Defaults to the current time.

    Returns:
        ``None`` when no call is in flight. ``"up"`` when **any** call advanced its last-byte
        stamp within *silence_threshold_s*. ``"down"`` only when **no** call did.
    """
    if not calls:
        return None
    at = time.monotonic() if now is None else now
    if any(call.silence_s(at) <= silence_threshold_s for call in calls):
        return "up"
    return "down"


def clear_cache() -> None:
    """Reset the module-level cache (test helper).

    Only intended for use in unit tests to ensure probe tests start from a
    clean state. Do not call from production code.
    """
    global _cached_snapshot
    _cached_snapshot = None
