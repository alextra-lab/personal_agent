"""Per-session Captain's Log reflection cadence gate (FRE-710).

Reflection previously fired unconditionally on every completed turn (~0.7 reflections/turn live,
flooding the promotion funnel: 1864 ``AWAITING_APPROVAL`` vs 6 ``Approved`` all-time). No durable
session-end signal exists anywhere in the codebase — ``SESSION_CLOSED``
(``telemetry/events.py``) is defined but never emitted, and neither ``Session`` nor
``SessionManager`` (``orchestrator/session.py``) track a turn count or a closed flag. This gate
approximates "once per session" with a per-``session_id`` minimum-interval debounce instead: the
first turn seen for a session always reflects, and subsequent turns for that session only reflect
once ``min_interval_seconds`` has elapsed since the last one. A turn that hits the iteration limit
always bypasses the interval, so the one already-computed "notable turn" signal that mattered under
the old per-turn cadence still surfaces.

This is process-local, in-memory state — mirroring ``SessionManager``'s own in-memory-only model, not
a new persistence layer.
"""

from __future__ import annotations

import dataclasses
import time

# How long a session's entry survives in the map with no reflected turn before it's evicted, as a
# multiple of min_interval_seconds. Generous enough that a session legitimately debouncing across
# the interval is never evicted mid-window; only sessions that have gone quiet for well past their
# own interval are pruned. Bounds the map's size by recently-active sessions rather than growing
# unboundedly over the gateway process's lifetime.
_EVICTION_MULTIPLIER = 4.0


@dataclasses.dataclass
class ReflectionCadenceGate:
    """Per-session, process-local reflection cadence gate.

    Attributes:
        min_interval_seconds: Minimum seconds between two reflected turns for the same
            ``session_id``.
    """

    min_interval_seconds: float
    _last_reflected_at: dict[str, float] = dataclasses.field(default_factory=dict)

    def should_reflect(
        self, session_id: str, *, hit_iteration_limit: bool, now: float | None = None
    ) -> bool:
        """Decide whether this turn should trigger a Captain's Log reflection.

        A turn is reflected when it is notable (``hit_iteration_limit``) or when no reflection has
        fired for this session within ``min_interval_seconds`` (the first turn of a session, or the
        interval has elapsed since the last one). A ``True`` result records ``now`` against
        ``session_id`` as a side effect.

        Synchronous / no ``await`` anywhere in this method by design — that (not a lock) is what
        makes the check-then-set against the shared dict safe under asyncio's single-threaded
        cooperative scheduling; a future edit must not make this a coroutine without
        re-establishing that safety some other way.

        Every call also opportunistically prunes entries older than
        ``min_interval_seconds * _EVICTION_MULTIPLIER``, bounding the dict's size by recently-active
        sessions.

        Args:
            session_id: The session this turn belongs to.
            hit_iteration_limit: Whether the turn hit the iteration-limit signal.
            now: The current timestamp (seconds); defaults to ``time.time()``. Injectable for
                deterministic tests.

        Returns:
            Whether a reflection should fire for this turn.
        """
        current = time.time() if now is None else now
        eviction_after = self.min_interval_seconds * _EVICTION_MULTIPLIER
        self._last_reflected_at = {
            sid: t for sid, t in self._last_reflected_at.items() if current - t < eviction_after
        }

        if hit_iteration_limit:
            self._last_reflected_at[session_id] = current
            return True

        last = self._last_reflected_at.get(session_id)
        if last is None or (current - last) >= self.min_interval_seconds:
            self._last_reflected_at[session_id] = current
            return True
        return False


_gate: ReflectionCadenceGate | None = None


def get_reflection_cadence_gate() -> ReflectionCadenceGate:
    """Return the process-global reflection cadence gate (lazy singleton).

    Returns:
        The process-wide :class:`ReflectionCadenceGate`, constructed on first use from
        ``settings.captains_log_reflection_min_interval_seconds``.
    """
    global _gate
    if _gate is None:
        from personal_agent.config import settings

        _gate = ReflectionCadenceGate(
            min_interval_seconds=settings.captains_log_reflection_min_interval_seconds
        )
    return _gate


def reset_reflection_cadence_gate() -> None:
    """Reset the process-global gate to an uninitialized state (test isolation only)."""
    global _gate
    _gate = None
