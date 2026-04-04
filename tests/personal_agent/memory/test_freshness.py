"""Unit tests for freshness decay and staleness classification (FRE-165 / ADR-0042 Step 5)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from personal_agent.memory.freshness import StalenessTier, classify_staleness, compute_freshness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _days_ago(days: float) -> datetime:
    return _utc_now() - timedelta(days=days)


def _mock_settings(
    half_life_days: float = 30.0,
    cold_threshold_days: float = 180.0,
) -> MagicMock:
    s = MagicMock()
    s.freshness_half_life_days = half_life_days
    s.freshness_cold_threshold_days = cold_threshold_days
    return s


# ---------------------------------------------------------------------------
# compute_freshness — zero-access guard
# ---------------------------------------------------------------------------


def test_compute_freshness_zero_access_count_returns_zero() -> None:
    """No accesses → freshness = 0.0 regardless of timestamp."""
    result = compute_freshness(
        last_accessed_at=_days_ago(1),
        access_count=0,
        half_life_days=30.0,
        alpha=0.1,
        max_boost=1.5,
    )
    assert result == 0.0


def test_compute_freshness_none_timestamp_returns_zero() -> None:
    """None timestamp → freshness = 0.0 regardless of access count."""
    result = compute_freshness(
        last_accessed_at=None,
        access_count=10,
        half_life_days=30.0,
        alpha=0.1,
        max_boost=1.5,
    )
    assert result == 0.0


def test_compute_freshness_both_missing_returns_zero() -> None:
    """Neither timestamp nor count → freshness = 0.0."""
    result = compute_freshness(
        last_accessed_at=None,
        access_count=0,
        half_life_days=30.0,
        alpha=0.1,
        max_boost=1.5,
    )
    assert result == 0.0


# ---------------------------------------------------------------------------
# compute_freshness — ADR-0042 example values
# ---------------------------------------------------------------------------


def test_compute_freshness_yesterday_many_accesses_capped_at_one() -> None:
    """ADR example: yesterday + 50 accesses → ~1.46, capped to 1.0."""
    result = compute_freshness(
        last_accessed_at=_days_ago(1),
        access_count=50,
        half_life_days=30.0,
        alpha=0.1,
        max_boost=1.5,
    )
    assert result == pytest.approx(1.0)


def test_compute_freshness_thirty_days_five_accesses() -> None:
    """ADR example: 30 days + 5 accesses → freshness ≈ 0.59."""
    result = compute_freshness(
        last_accessed_at=_days_ago(30),
        access_count=5,
        half_life_days=30.0,
        alpha=0.1,
        max_boost=1.5,
    )
    # base_decay at 30 days = e^(-ln2) = 0.5
    # boost = min(1 + 0.1 * ln(6), 1.5) = min(1 + 0.1795, 1.5) = 1.1795
    # freshness = 0.5 * 1.1795 ≈ 0.59
    assert result == pytest.approx(0.59, abs=0.02)


def test_compute_freshness_ninety_days_one_access() -> None:
    """ADR example: 90 days + 1 access → freshness ≈ 0.13."""
    result = compute_freshness(
        last_accessed_at=_days_ago(90),
        access_count=1,
        half_life_days=30.0,
        alpha=0.1,
        max_boost=1.5,
    )
    # base_decay at 90 days = e^(-3*ln2) = 0.5^3 = 0.125
    # boost = min(1 + 0.1 * ln(2), 1.5) = min(1 + 0.0693, 1.5) = 1.0693
    # freshness = 0.125 * 1.0693 ≈ 0.134
    assert result == pytest.approx(0.13, abs=0.015)


def test_compute_freshness_decay_at_half_life_without_boost() -> None:
    """At exactly half_life with access_count=1 and alpha=0.0, base_decay = 0.5."""
    result = compute_freshness(
        last_accessed_at=_days_ago(30),
        access_count=1,
        half_life_days=30.0,
        alpha=0.0,
        max_boost=1.5,
    )
    # With alpha=0, boost = min(1.0 + 0, 1.5) = 1.0 exactly
    # base_decay = 0.5
    assert result == pytest.approx(0.5, rel=1e-4)


def test_compute_freshness_capped_at_one() -> None:
    """Result never exceeds 1.0 even with extreme boost."""
    result = compute_freshness(
        last_accessed_at=_days_ago(0.001),  # very recent
        access_count=1000,
        half_life_days=30.0,
        alpha=1.0,
        max_boost=2.0,
    )
    assert result <= 1.0


def test_compute_freshness_in_range() -> None:
    """Result is always in [0.0, 1.0]."""
    for days in [0.1, 1, 7, 30, 90, 365]:
        for count in [1, 5, 100]:
            r = compute_freshness(
                last_accessed_at=_days_ago(days),
                access_count=count,
                half_life_days=30.0,
                alpha=0.1,
                max_boost=1.5,
            )
            assert 0.0 <= r <= 1.0, f"Out of range for days={days}, count={count}: {r}"


def test_compute_freshness_naive_datetime_treated_as_utc() -> None:
    """Naive datetimes (no tzinfo) are treated as UTC without error."""
    naive_dt = datetime.utcnow() - timedelta(days=1)
    result = compute_freshness(
        last_accessed_at=naive_dt,
        access_count=5,
        half_life_days=30.0,
        alpha=0.1,
        max_boost=1.5,
    )
    assert result > 0.0


def test_compute_freshness_zero_half_life_guard() -> None:
    """half_life_days <= 0 falls back to 1.0 without raising."""
    result = compute_freshness(
        last_accessed_at=_days_ago(1),
        access_count=1,
        half_life_days=0.0,  # misconfigured
        alpha=0.1,
        max_boost=1.5,
    )
    # Fallback to 1.0 half_life → very rapid decay for 1-day-old access
    assert 0.0 <= result <= 1.0


# ---------------------------------------------------------------------------
# classify_staleness — tier boundaries
# ---------------------------------------------------------------------------


def test_classify_staleness_warm() -> None:
    """Accessed within half_life_days → WARM."""
    settings = _mock_settings(half_life_days=30.0)
    tier = classify_staleness(
        last_accessed_at=_days_ago(10),
        access_count=5,
        created_at=_days_ago(60),
        settings=settings,
    )
    assert tier == StalenessTier.WARM


def test_classify_staleness_cooling() -> None:
    """Accessed between half_life and 2×half_life → COOLING."""
    settings = _mock_settings(half_life_days=30.0)
    tier = classify_staleness(
        last_accessed_at=_days_ago(45),
        access_count=3,
        created_at=_days_ago(100),
        settings=settings,
    )
    assert tier == StalenessTier.COOLING


def test_classify_staleness_cold() -> None:
    """Accessed between 2×half_life and cold_threshold → COLD."""
    settings = _mock_settings(half_life_days=30.0, cold_threshold_days=180.0)
    tier = classify_staleness(
        last_accessed_at=_days_ago(90),
        access_count=1,
        created_at=_days_ago(120),
        settings=settings,
    )
    assert tier == StalenessTier.COLD


def test_classify_staleness_dormant_old_access() -> None:
    """Accessed more than cold_threshold_days ago → DORMANT."""
    settings = _mock_settings(half_life_days=30.0, cold_threshold_days=180.0)
    tier = classify_staleness(
        last_accessed_at=_days_ago(200),
        access_count=1,
        created_at=_days_ago(300),
        settings=settings,
    )
    assert tier == StalenessTier.DORMANT


def test_classify_staleness_never_accessed_old_node() -> None:
    """Never-accessed node created > cold_threshold ago → DORMANT."""
    settings = _mock_settings(half_life_days=30.0, cold_threshold_days=180.0)
    tier = classify_staleness(
        last_accessed_at=None,
        access_count=0,
        created_at=_days_ago(200),
        settings=settings,
    )
    assert tier == StalenessTier.DORMANT


def test_classify_staleness_never_accessed_new_node() -> None:
    """Never-accessed node created recently → COLD (not DORMANT)."""
    settings = _mock_settings(half_life_days=30.0, cold_threshold_days=180.0)
    tier = classify_staleness(
        last_accessed_at=None,
        access_count=0,
        created_at=_days_ago(10),
        settings=settings,
    )
    assert tier == StalenessTier.COLD


def test_classify_staleness_never_accessed_no_created_at() -> None:
    """Never-accessed node with no created_at → DORMANT (safest assumption)."""
    settings = _mock_settings(half_life_days=30.0, cold_threshold_days=180.0)
    tier = classify_staleness(
        last_accessed_at=None,
        access_count=0,
        created_at=None,
        settings=settings,
    )
    assert tier == StalenessTier.DORMANT


def test_classify_staleness_just_inside_half_life() -> None:
    """Accessed just inside half_life_days → WARM."""
    settings = _mock_settings(half_life_days=30.0)
    tier = classify_staleness(
        last_accessed_at=_days_ago(29),
        access_count=1,
        created_at=_days_ago(60),
        settings=settings,
    )
    assert tier == StalenessTier.WARM


def test_classify_staleness_just_inside_double_half_life() -> None:
    """Accessed between half_life and 2×half_life → COOLING."""
    settings = _mock_settings(half_life_days=30.0)
    tier = classify_staleness(
        last_accessed_at=_days_ago(59),
        access_count=1,
        created_at=_days_ago(90),
        settings=settings,
    )
    assert tier == StalenessTier.COOLING


def test_classify_staleness_naive_datetime() -> None:
    """Naive datetimes (no tzinfo) are handled without error."""
    settings = _mock_settings(half_life_days=30.0)
    naive_last = datetime.utcnow() - timedelta(days=5)
    naive_created = datetime.utcnow() - timedelta(days=60)
    tier = classify_staleness(
        last_accessed_at=naive_last,
        access_count=2,
        created_at=naive_created,
        settings=settings,
    )
    assert tier == StalenessTier.WARM


# ---------------------------------------------------------------------------
# StalenessTier — enum values
# ---------------------------------------------------------------------------


def test_staleness_tier_string_values() -> None:
    """StalenessTier is a str-Enum with expected values."""
    assert StalenessTier.WARM == "warm"
    assert StalenessTier.COOLING == "cooling"
    assert StalenessTier.COLD == "cold"
    assert StalenessTier.DORMANT == "dormant"
