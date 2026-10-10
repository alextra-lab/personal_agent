"""Request-receipt clock for the turn's time to first token (ADR-0154 D6, FRE-1512).

The service stamps the instant it receives a chat request. After the first user-visible
push it reads the elapsed time and records it on the turn's route-trace row. While the
clock is active, the observation seam leaves the turn-level Elasticsearch document to the
service: the service writes it once, after the first-token update, so two unordered
full-document writes can never race on one document id.

The clock is a ``ContextVar``, so each request task carries its own value.
"""

from __future__ import annotations

import time
from contextvars import ContextVar

_received_monotonic: ContextVar[float | None] = ContextVar("first_token_received", default=None)


def start_first_token_clock(received: float | None = None) -> float:
    """Start the clock for the current request task.

    Args:
        received: A ``time.monotonic()`` reading taken at request receipt. ``None``
            reads the clock now.

    Returns:
        The receipt reading now held by the clock.
    """
    value = time.monotonic() if received is None else received
    _received_monotonic.set(value)
    return value


def clear_first_token_clock() -> None:
    """Stop the clock for the current request task."""
    _received_monotonic.set(None)


def first_token_clock_active() -> bool:
    """Return whether a request in this task has a running first-token clock."""
    return _received_monotonic.get() is not None


def request_elapsed_ms() -> float | None:
    """Return the milliseconds from request receipt to now, or ``None`` with no running clock.

    The route-trace seam reads this when it writes the durable row (FRE-1568), so the row
    carries the whole-turn time from the same start point the retired ``RequestTimer`` used.

    Returns:
        Elapsed time in milliseconds, or ``None`` when this task has no running clock.
    """
    received = _received_monotonic.get()
    return None if received is None else elapsed_ms(received)


def elapsed_ms(received: float) -> float:
    """Return the milliseconds from a receipt reading to now.

    Args:
        received: A ``time.monotonic()`` reading taken at request receipt.

    Returns:
        Elapsed time in milliseconds.
    """
    return (time.monotonic() - received) * 1000.0
