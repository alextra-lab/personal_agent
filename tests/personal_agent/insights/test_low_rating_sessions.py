"""Tests for detect_low_rating_sessions in InsightsEngine (FRE-407)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from personal_agent.insights.engine import InsightsEngine

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_engine() -> InsightsEngine:
    """Build an InsightsEngine with mocked dependencies."""
    engine = InsightsEngine()
    # Prevent real Neo4j / Postgres calls
    engine._memory = AsyncMock()  # type: ignore[assignment]
    engine._memory.connected = False
    engine._cost_tracker = AsyncMock()  # type: ignore[assignment]
    return engine


def _rating_bucket(callsite: str, mean_rating: float, count: int) -> dict:
    return {"callsite": callsite, "mean_rating": mean_rating, "count": count}


# ---------------------------------------------------------------------------
# Min-count floor (codex): callsite with count < 5 is NOT flagged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMinCountFloor:
    async def test_count_4_not_flagged(self) -> None:
        """count=4 (below floor=5) → no insight even if mean < 1.5."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[_rating_bucket("orchestrator.primary", 0.5, 4)]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert insights == []

    async def test_count_5_flagged(self) -> None:
        """count=5 (at floor) with mean < 1.5 → flagged."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[_rating_bucket("orchestrator.primary", 1.0, 5)]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert len(insights) == 1
        assert insights[0].insight_type == "low_rating"
        assert insights[0].evidence["count"] == 5

    async def test_count_6_flagged(self) -> None:
        """count=6 (above floor) with mean < 1.5 → flagged."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[_rating_bucket("orchestrator.primary", 1.2, 6)]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert len(insights) == 1
        assert insights[0].evidence["count"] == 6


# ---------------------------------------------------------------------------
# Mean threshold: mean >= 1.5 is NOT flagged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMeanThreshold:
    async def test_mean_exactly_1_5_not_flagged(self) -> None:
        """mean=1.5 (at threshold boundary) → not flagged."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[_rating_bucket("orchestrator.primary", 1.5, 10)]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert insights == []

    async def test_mean_below_1_5_flagged(self) -> None:
        """mean=1.4 (below threshold) with count >= 5 → flagged."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[_rating_bucket("role.primary", 1.4, 10)]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert len(insights) == 1
        ins = insights[0]
        assert ins.evidence["prompt_callsite"] == "role.primary"
        assert abs(float(ins.evidence["mean_rating"]) - 1.4) < 1e-6

    async def test_healthy_mean_not_flagged(self) -> None:
        """mean=2.5 → not flagged regardless of count."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[_rating_bucket("orchestrator.primary", 2.5, 20)]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert insights == []


# ---------------------------------------------------------------------------
# Multiple callsites: only failing ones are flagged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMultipleCallsites:
    async def test_only_bad_callsite_flagged(self) -> None:
        """Two callsites: one bad, one healthy → only bad one emits insight."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[
                _rating_bucket("orchestrator.primary", 0.8, 8),  # bad
                _rating_bucket("role.primary", 2.0, 12),  # healthy
            ]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert len(insights) == 1
        assert insights[0].evidence["prompt_callsite"] == "orchestrator.primary"

    async def test_unknown_callsite_excluded(self) -> None:
        """Null / 'unknown' callsite (null denorm) is excluded from per-callsite flags."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[
                _rating_bucket("unknown", 0.1, 20),  # no prompt identity — excluded
            ]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert insights == []


# ---------------------------------------------------------------------------
# ES unavailable / query failure → empty, no exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEsUnavailable:
    async def test_es_failure_returns_empty(self) -> None:
        """ES query failure → empty list (no exception propagated)."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            side_effect=Exception("ES connection refused")
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert insights == []
