"""Retry pacing for the session-digest idle sweep (FRE-987, ADR-0124 Amendment D).

The rule under test: a failure is paced, never retired. Either the caller knows the
instant the failing condition clears — a budget denial carries its cap's window reset —
or the delay grows exponentially with consecutive failures and saturates at a ceiling.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from personal_agent.memory.session_digest import next_retry_after

_NOW = datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc)
_BASE = 900.0
_MAX = 21_600.0


@pytest.mark.parametrize(
    ("attempt_index", "expected_seconds"),
    [
        (1, 900.0),  # 15 min — a genuine blip is not punished
        (2, 1_800.0),
        (3, 3_600.0),
        (4, 7_200.0),
        (5, 14_400.0),
        (6, 21_600.0),  # saturated
        (7, 21_600.0),
        (390, 21_600.0),  # the live corpus reached 390 attempts; must not overflow
    ],
)
def test_backoff_grows_and_saturates(attempt_index: int, expected_seconds: float) -> None:
    """Delay doubles per consecutive failure and stops at the ceiling.

    The 390th case is the one the incident produced. Computing ``base * 2 ** 389`` in
    floating point raises rather than saturating, so the shift is clamped before the
    multiply — a session that has failed all week must still be *paced*, not crash the
    sweep that is pacing it.
    """
    assert next_retry_after(
        attempt_index=attempt_index, now=_NOW, base_seconds=_BASE, max_seconds=_MAX
    ) == _NOW + timedelta(seconds=expected_seconds)


def test_an_attempt_index_below_one_is_treated_as_the_first() -> None:
    """Defensive: a miscounted index must not produce a delay shorter than the base."""
    assert next_retry_after(
        attempt_index=0, now=_NOW, base_seconds=_BASE, max_seconds=_MAX
    ) == _NOW + timedelta(seconds=_BASE)


def test_a_known_clearing_instant_wins_over_the_backoff() -> None:
    """A budget denial retries when its window resets, not on the sweep clock.

    This is the whole of FRE-987's "no awareness of when the condition could plausibly
    clear": the cap rolls over at a known instant, so retrying twelve times an hour
    against it is waste with a knowable end.
    """
    resets_at = _NOW + timedelta(hours=14)

    assert (
        next_retry_after(
            attempt_index=1,
            now=_NOW,
            base_seconds=_BASE,
            max_seconds=_MAX,
            condition_clears_at=resets_at,
        )
        == resets_at
    )


def test_a_clearing_instant_already_past_falls_back_to_the_backoff() -> None:
    """A stale or skewed reset instant must not make the delay a no-op.

    Returning a past instant would leave the session immediately eligible again — the
    exact every-tick behaviour this pacing exists to remove.
    """
    assert next_retry_after(
        attempt_index=2,
        now=_NOW,
        base_seconds=_BASE,
        max_seconds=_MAX,
        condition_clears_at=_NOW - timedelta(minutes=5),
    ) == _NOW + timedelta(seconds=1_800.0)


def test_a_naive_clearing_instant_is_read_as_utc() -> None:
    """Timestamps cross a store boundary as ISO strings; a naive one is not a crash."""
    resets_at = datetime(2026, 7, 29, 0, 0, 0)

    assert next_retry_after(
        attempt_index=1,
        now=_NOW,
        base_seconds=_BASE,
        max_seconds=_MAX,
        condition_clears_at=resets_at,
    ) == resets_at.replace(tzinfo=timezone.utc)
