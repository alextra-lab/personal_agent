"""Tests for freshness review cron parsing (FRE-166)."""

from personal_agent.brainstem.jobs.freshness_review import parse_freshness_review_schedule


def test_parse_default_sunday_0300_utc() -> None:
    """``0 3 * * 0`` maps to Sunday 03:00 in Python weekday convention."""
    m, h, wd = parse_freshness_review_schedule("0 3 * * 0")
    assert m == 0
    assert h == 3
    assert wd == 6  # Sunday


def test_parse_monday_1430() -> None:
    """Monday 14:30 UTC."""
    m, h, wd = parse_freshness_review_schedule("30 14 * * 1")
    assert m == 30
    assert h == 14
    assert wd == 0  # Monday


def test_parse_sunday_alt_7() -> None:
    """Some crons use 7 for Sunday."""
    m, h, wd = parse_freshness_review_schedule("0 4 * * 7")
    assert m == 0
    assert h == 4
    assert wd == 6


def test_parse_invalid_falls_back() -> None:
    """Malformed cron falls back to default window."""
    m, h, wd = parse_freshness_review_schedule("not a cron")
    assert (m, h, wd) == (0, 3, 6)
