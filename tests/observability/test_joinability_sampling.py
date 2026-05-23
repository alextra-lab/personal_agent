"""Unit tests for :mod:`personal_agent.observability.joinability.sampling`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from personal_agent.observability.joinability.sampling import (
    pick_session,
    seed_for,
)


def test_seed_for_rounds_to_hour() -> None:
    t1 = datetime(2026, 5, 23, 14, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 23, 14, 59, 59, tzinfo=timezone.utc)
    t3 = datetime(2026, 5, 23, 15, 0, 0, tzinfo=timezone.utc)
    assert seed_for(t1) == seed_for(t2)
    assert seed_for(t1) != seed_for(t3)
    # Sanity: seed_for(t3) is exactly one hour later.
    assert seed_for(t3) - seed_for(t1) == 3600


def test_seed_for_hour_boundary_inclusive() -> None:
    """T == hh:00:00 belongs to that hour's bucket."""
    t = datetime(2026, 5, 23, 14, 0, 0, tzinfo=timezone.utc)
    s = seed_for(t)
    assert s == int(t.timestamp())


def test_pick_session_returns_none_on_empty_pool() -> None:
    assert pick_session([], seed=0) is None


def test_pick_session_is_deterministic_per_seed() -> None:
    pool = ["s1", "s2", "s3", "s4", "s5"]
    a = pick_session(pool, seed=1_748_016_000)
    b = pick_session(pool, seed=1_748_016_000)
    assert a == b
    assert a in pool


def test_pick_session_varies_with_seed() -> None:
    pool = [f"s{i:03d}" for i in range(50)]
    picks = {pick_session(pool, seed=s) for s in range(50)}
    # With 50 seeds against 50 elements, we expect strong variation.
    # Use >5 unique picks as a sanity floor that wouldn't be hit by a
    # broken (constant-output) implementation.
    assert len(picks) > 5


def test_pick_session_single_element_returns_it() -> None:
    assert pick_session(["only-one"], seed=42) == "only-one"


def test_seed_drift_across_hours() -> None:
    """An hour-by-hour walk produces strictly-increasing distinct seeds."""
    base = datetime(2026, 5, 23, 0, 0, 0, tzinfo=timezone.utc)
    seeds = [seed_for(base + timedelta(hours=h)) for h in range(24)]
    assert len(set(seeds)) == 24
    assert seeds == sorted(seeds)
