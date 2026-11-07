# ruff: noqa: D102
"""The window arithmetic of the broker (ADR-0159 D5.5, D5.11; FRE-1683)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from personal_agent.dev_broker.clock import month_start, seconds_until_safe

_UTC = timezone.utc


class TestMonthStart:
    def test_last_minute_of_a_month_is_that_month(self) -> None:
        assert month_start(datetime(2026, 11, 30, 23, 59, tzinfo=_UTC)) == date(2026, 11, 1)

    def test_first_instant_of_a_month_is_the_new_month(self) -> None:
        assert month_start(datetime(2026, 12, 1, 0, 0, tzinfo=_UTC)) == date(2026, 12, 1)

    def test_a_non_utc_instant_is_read_in_utc(self) -> None:
        # 00:30 on 1 December in UTC+1 is 23:30 on 30 November in UTC.
        from datetime import timedelta

        cet = timezone(timedelta(hours=1))
        assert month_start(datetime(2026, 12, 1, 0, 30, tzinfo=cet)) == date(2026, 11, 1)


class TestSecondsUntilSafe:
    def test_far_from_the_boundary_is_safe(self) -> None:
        assert seconds_until_safe(datetime(2026, 11, 7, 12, 0, tzinfo=_UTC), 150) == 0.0

    def test_exactly_the_guard_before_midnight_is_safe(self) -> None:
        now = datetime(2026, 11, 7, 23, 57, 30, tzinfo=_UTC)
        assert seconds_until_safe(now, 150) == 0.0

    def test_100_seconds_before_midnight_waits_past_the_boundary(self) -> None:
        now = datetime(2026, 11, 7, 23, 58, 20, tzinfo=_UTC)
        wait = seconds_until_safe(now, 150)
        assert wait > 100.0
        assert wait <= 101.0

    def test_the_weekly_boundary_is_a_daily_boundary(self) -> None:
        # Monday 2026-11-09 00:00 UTC starts a cost-gate week.
        now = datetime(2026, 11, 8, 23, 59, 0, tzinfo=_UTC)
        assert 60.0 < seconds_until_safe(now, 150) <= 61.0

    def test_just_after_midnight_is_safe(self) -> None:
        now = datetime(2026, 11, 8, 0, 0, 1, tzinfo=_UTC)
        assert seconds_until_safe(now, 150) == 0.0
