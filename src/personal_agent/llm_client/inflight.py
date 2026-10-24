"""Process-level registry of in-flight local model calls (FRE-1433, ADR-0144 D4).

The question this answers: has a live local call gone silent, and for how long. Nothing else can.
:class:`~personal_agent.llm_client.types.GenerationProgress` is caller-owned, stamps its clock on the
first content chunk, and ignores reasoning deltas. A call that has received nothing has no
timestamp there, and a thinking phase reads as silence.

Each record holds two stamps:

* ``dispatched_monotonic`` is taken when the request is issued. A call with no chunk yet has an age
  from this stamp.
* ``last_byte_monotonic`` advances on **any** chunk the stream yields: content, reasoning, tool-call
  and prefill-progress chunks alike. It starts equal to the dispatch stamp, so a chunkless call is
  silent from dispatch and a consumer never handles ``None``.

The key is the model call's own ``span_id`` plus the attempt ordinal within that call. One trace holds
many model calls, each with its own attempt 1, so a trace-keyed ordinal collides.

This is **not** ADR-0143's turn registry. That one is keyed by trace and holds a turn's ``started_at``.
Turn age cannot tell a healthy 541-second turn from a wedged one. Keep them apart.

The registry is process-local and takes no lock. It is sound only because Seshat runs one worker
with one event loop, the invariant ADR-0143 D1 also relies on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

__all__ = [
    "InflightCall",
    "InflightCallRegistry",
    "InflightKey",
    "get_inflight_registry",
    "reset_inflight_registry",
]

InflightKey = tuple[str, int]
"""``(span_id, attempt)`` — unique per attempt of one model call."""


@dataclass(frozen=True)
class InflightCall:
    """One live attempt of a local model call.

    Attributes:
        span_id: The model call's own span id.
        attempt: The attempt ordinal within that call, starting at 1.
        dispatched_monotonic: ``time.monotonic()`` when the request was issued.
        last_byte_monotonic: ``time.monotonic()`` of the latest chunk, or the dispatch stamp
            when none has arrived.
    """

    span_id: str
    attempt: int
    dispatched_monotonic: float
    last_byte_monotonic: float

    def age_s(self, now: float | None = None) -> float:
        """Return the seconds since dispatch.

        Args:
            now: A ``time.monotonic()`` reading. Defaults to the current time.

        Returns:
            Seconds since the request was issued.
        """
        return (time.monotonic() if now is None else now) - self.dispatched_monotonic

    def silence_s(self, now: float | None = None) -> float:
        """Return the seconds since the latest chunk, or since dispatch if none arrived.

        Args:
            now: A ``time.monotonic()`` reading. Defaults to the current time.

        Returns:
            Seconds of silence.
        """
        return (time.monotonic() if now is None else now) - self.last_byte_monotonic


class InflightCallRegistry:
    """The live local attempts of this process, keyed by ``(span_id, attempt)``."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._calls: dict[InflightKey, InflightCall] = {}

    def register(self, span_id: str, attempt: int) -> InflightKey:
        """Record a dispatched attempt, with both stamps set to now.

        Args:
            span_id: The model call's own span id.
            attempt: The attempt ordinal within that call.

        Returns:
            The key to pass to :meth:`touch` and :meth:`remove`.
        """
        now = time.monotonic()
        key = (span_id, attempt)
        self._calls[key] = InflightCall(
            span_id=span_id, attempt=attempt, dispatched_monotonic=now, last_byte_monotonic=now
        )
        return key

    def touch(self, key: InflightKey) -> None:
        """Advance an attempt's last-byte stamp. Does nothing if the attempt has ended.

        Args:
            key: The key that :meth:`register` returned.
        """
        call = self._calls.get(key)
        if call is not None:
            self._calls[key] = replace(call, last_byte_monotonic=time.monotonic())

    def remove(self, key: InflightKey) -> None:
        """Drop an attempt. Safe to call twice.

        Args:
            key: The key that :meth:`register` returned.
        """
        self._calls.pop(key, None)

    def get(self, key: InflightKey) -> InflightCall | None:
        """Return one attempt's record, or ``None`` if it is not live.

        Args:
            key: The key that :meth:`register` returned.

        Returns:
            The record, or ``None``.
        """
        return self._calls.get(key)

    def snapshot(self) -> tuple[InflightCall, ...]:
        """Return every live attempt.

        Returns:
            A tuple copy, so a caller may hold it across an ``await``.
        """
        return tuple(self._calls.values())


_registry = InflightCallRegistry()


def get_inflight_registry() -> InflightCallRegistry:
    """Return the process-wide registry.

    Returns:
        The one :class:`InflightCallRegistry` of this process.
    """
    return _registry


def reset_inflight_registry() -> None:
    """Replace the process-wide registry with an empty one. For tests."""
    global _registry
    _registry = InflightCallRegistry()
