"""Time for the broker: an injectable clock and the window arithmetic (ADR-0159 D5.5, D5.11).

Every time value that the counters use comes from a :class:`Clock`, so a test can put a call at
23:59 UTC on a month's last day, or 100 seconds before a cost-gate boundary.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """The source of time for the broker."""

    def now(self) -> datetime:
        """Return the current instant, timezone-aware."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait for ``seconds``."""
        ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        """Return the current UTC instant."""
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        """Wait for ``seconds`` of real time."""
        await asyncio.sleep(seconds)


def month_start(now: datetime) -> date:
    """Return the first day of the UTC calendar month that holds ``now``.

    Args:
        now: A timezone-aware instant.

    Returns:
        The first day of that month in UTC.
    """
    utc = now.astimezone(timezone.utc)
    return date(utc.year, utc.month, 1)


def seconds_until_safe(now: datetime, guard_seconds: float) -> float:
    """Return how long a call must wait so that it cannot cross a cost-gate window boundary.

    The cost-gate windows are daily and weekly, and both start at 00:00 UTC (a week starts on a
    Monday), so the next UTC midnight is the next boundary. A call that starts less than
    ``guard_seconds`` before it waits until one second after it.

    Args:
        now: A timezone-aware instant.
        guard_seconds: The shortest time that must remain before the boundary.

    Returns:
        ``0.0`` when the call can start now, else the seconds to wait.
    """
    utc = now.astimezone(timezone.utc)
    next_midnight = datetime(utc.year, utc.month, utc.day, tzinfo=timezone.utc) + timedelta(days=1)
    remaining = (next_midnight - utc).total_seconds()
    if remaining >= guard_seconds:
        return 0.0
    return remaining + 1.0
