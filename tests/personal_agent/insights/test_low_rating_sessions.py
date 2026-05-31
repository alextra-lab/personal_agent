"""Tests for detect_low_rating_sessions in InsightsEngine (FRE-407).

Covers:
  - Imputed mean formula: (rated_sum + 2 * unrated) / total_turns
  - Floor on total_turns >= 5 (population), not rated_count
  - No-ratings callsites impute to 2.0 → never flagged
  - ES failure → empty list, no exception
"""

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


def _imputed_bucket(
    callsite: str,
    rated_count: int,
    rated_sum: float,
    total_turns: int,
) -> dict:
    """Build a bucket dict matching the new get_low_rating_buckets contract."""
    return {
        "callsite": callsite,
        "rated_count": rated_count,
        "rated_sum": rated_sum,
        "total_turns": total_turns,
    }


# ---------------------------------------------------------------------------
# Imputation scenarios (specified in FRE-407 refinement)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestImputedMean:
    async def test_two_zeros_eight_unrated_not_flagged(self) -> None:
        """total=10, 2 explicit 0-ratings, 8 un-rated → imputed=(0+16)/10=1.6 → NOT flagged.

        imputed_mean=1.6 >= 1.5 threshold → no insight.
        """
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[
                _imputed_bucket(
                    "orchestrator.primary", rated_count=2, rated_sum=0.0, total_turns=10
                )
            ]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert insights == [], (
            "imputed_mean=1.6 is not below threshold 1.5; callsite must NOT be flagged"
        )

    async def test_six_zeros_four_unrated_flagged(self) -> None:
        """total=10, 6 explicit 0-ratings, 4 un-rated → imputed=(0+8)/10=0.8 → FLAGGED."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[
                _imputed_bucket(
                    "orchestrator.primary", rated_count=6, rated_sum=0.0, total_turns=10
                )
            ]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert len(insights) == 1
        ev = insights[0].evidence
        assert ev["prompt_callsite"] == "orchestrator.primary"
        assert abs(ev["imputed_mean"] - 0.8) < 1e-6, f"expected 0.8, got {ev['imputed_mean']}"
        assert ev["rated_count"] == 6
        assert ev["total_turns"] == 10

    async def test_three_zeros_total_three_below_floor(self) -> None:
        """total=3, 3 explicit 0-ratings → below floor (total_turns < 5) → NOT flagged."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[
                _imputed_bucket("orchestrator.primary", rated_count=3, rated_sum=0.0, total_turns=3)
            ]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert insights == [], "total_turns=3 is below floor=5; must not be flagged"

    async def test_no_ratings_total_twenty_not_flagged(self) -> None:
        """total=20, 0 ratings → imputed=(0+40)/20=2.0 → NOT flagged (expected no-op)."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[
                _imputed_bucket("role.primary", rated_count=0, rated_sum=0.0, total_turns=20)
            ]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert insights == [], (
            "zero-rated callsite imputes to 2.0; must not be flagged even at high volume"
        )


# ---------------------------------------------------------------------------
# Floor logic: total_turns (population), not rated_count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPopulationFloor:
    async def test_total_turns_4_not_flagged(self) -> None:
        """total_turns=4 (below floor=5) → not flagged regardless of mean."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[
                _imputed_bucket("orchestrator.primary", rated_count=4, rated_sum=0.0, total_turns=4)
            ]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert insights == []

    async def test_total_turns_5_with_bad_imputed_flagged(self) -> None:
        """total_turns=5 (at floor), all 0-rated → imputed=(0+0)/5=0.0 → flagged."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[
                _imputed_bucket("orchestrator.primary", rated_count=5, rated_sum=0.0, total_turns=5)
            ]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert len(insights) == 1
        assert insights[0].evidence["total_turns"] == 5

    async def test_total_turns_5_good_imputed_not_flagged(self) -> None:
        """total_turns=5, 1 zero + 4 un-rated → imputed=(0+8)/5=1.6 → NOT flagged."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[
                _imputed_bucket("orchestrator.primary", rated_count=1, rated_sum=0.0, total_turns=5)
            ]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert insights == []


# ---------------------------------------------------------------------------
# Multiple callsites
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMultipleCallsites:
    async def test_only_bad_callsite_flagged(self) -> None:
        """One bad callsite (imputed=0.8), one healthy (imputed=2.0) → only bad flagged."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[
                # 6 zeros out of 10 → imputed = 0.8 → flagged
                _imputed_bucket(
                    "orchestrator.primary", rated_count=6, rated_sum=0.0, total_turns=10
                ),
                # 0 ratings out of 12 → imputed = 2.0 → not flagged
                _imputed_bucket("role.primary", rated_count=0, rated_sum=0.0, total_turns=12),
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
                _imputed_bucket("unknown", rated_count=20, rated_sum=0.0, total_turns=20),
            ]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert insights == []


# ---------------------------------------------------------------------------
# Threshold boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestImputedThreshold:
    async def test_imputed_exactly_1_5_not_flagged(self) -> None:
        """imputed_mean=1.5 (at boundary) → not flagged."""
        # (0*3 + 2*7) / 10 = 14/10 = 1.4 → flagged; use (0*2 + 2*8)/10=1.6 for >=1.5
        # For exactly 1.5: need (rated_sum + 2*unrated)/10 = 1.5 → rated_sum + 2*unrated = 15
        # e.g. rated_count=5, rated_sum=5.0, unrated=5 → (5+10)/10=1.5
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[
                _imputed_bucket(
                    "orchestrator.primary", rated_count=5, rated_sum=5.0, total_turns=10
                )
            ]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert insights == []

    async def test_imputed_just_below_1_5_flagged(self) -> None:
        """imputed_mean < 1.5 → flagged; 5 zeros + 5 unrated = (0+10)/10=1.0 → flagged."""
        engine = _make_engine()
        engine._queries = AsyncMock()  # type: ignore[assignment]
        engine._queries.get_low_rating_buckets = AsyncMock(
            return_value=[
                _imputed_bucket(
                    "orchestrator.primary", rated_count=5, rated_sum=0.0, total_turns=10
                )
            ]
        )

        insights = await engine.detect_low_rating_sessions(days=7)
        assert len(insights) == 1
        assert abs(insights[0].evidence["imputed_mean"] - 1.0) < 1e-6


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
