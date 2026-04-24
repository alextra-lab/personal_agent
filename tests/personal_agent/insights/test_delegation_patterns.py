"""Tests for delegation pattern analysis in InsightsEngine (ADR-0057 §D4)."""

from unittest.mock import AsyncMock

import pytest

from personal_agent.insights.engine import InsightsEngine


@pytest.mark.asyncio
class TestDetectDelegationPatternsEmpty:
    """Low-volume / ES-unavailable cases return empty list."""

    async def test_returns_empty_when_no_data(self) -> None:
        """No delegation records → no insights."""
        engine = InsightsEngine()
        engine._queries.get_delegation_pattern_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "total": 0,
                "successes": 0,
                "rounds_needed_values": [],
                "missing_context_terms": [],
            }
        )
        insights = await engine.detect_delegation_patterns(days=30)
        assert insights == []

    async def test_returns_empty_when_sample_below_success_threshold(self) -> None:
        """count < 10 skips success-rate and rounds aggregations."""
        engine = InsightsEngine()
        engine._queries.get_delegation_pattern_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "total": 9,
                "successes": 2,
                "rounds_needed_values": [5, 4, 6, 3, 2, 4, 5, 6, 3],
                "missing_context_terms": [("missing schema", 5)],
            }
        )
        insights = await engine.detect_delegation_patterns(days=30)
        kinds = {i.pattern_kind for i in insights}
        assert "delegation_success_rate" not in kinds
        assert "delegation_rounds" not in kinds
        assert "delegation_missing_context" in kinds


@pytest.mark.asyncio
class TestDetectDelegationPatternsSuccessRate:
    """Success-rate aggregation (ADR-0057 §D4)."""

    async def test_low_success_rate_produces_insight(self) -> None:
        """42% success rate (< 60%) and count ≥ 10 → delegation_success_rate insight."""
        engine = InsightsEngine()
        engine._queries.get_delegation_pattern_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "total": 31,
                "successes": 13,
                "rounds_needed_values": [1] * 31,
                "missing_context_terms": [],
            }
        )
        insights = await engine.detect_delegation_patterns(days=30)
        success_insights = [i for i in insights if i.pattern_kind == "delegation_success_rate"]
        assert len(success_insights) == 1
        ins = success_insights[0]
        assert ins.insight_type == "delegation"
        assert ins.evidence["total_delegations"] == 31
        assert ins.evidence["successful"] == 13
        assert abs(float(ins.evidence["success_rate"]) - round(13 / 31, 4)) < 1e-6
        assert 0.70 <= ins.confidence <= 0.90

    async def test_healthy_success_rate_skipped(self) -> None:
        """75% success rate (≥ 60%) → no success_rate insight."""
        engine = InsightsEngine()
        engine._queries.get_delegation_pattern_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "total": 20,
                "successes": 15,
                "rounds_needed_values": [1] * 20,
                "missing_context_terms": [],
            }
        )
        insights = await engine.detect_delegation_patterns(days=30)
        assert not any(i.pattern_kind == "delegation_success_rate" for i in insights)

    async def test_exactly_60_percent_success_rate_not_triggered(self) -> None:
        """Exact 60% success rate is not below threshold → no insight."""
        engine = InsightsEngine()
        engine._queries.get_delegation_pattern_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "total": 10,
                "successes": 6,
                "rounds_needed_values": [1] * 10,
                "missing_context_terms": [],
            }
        )
        insights = await engine.detect_delegation_patterns(days=30)
        assert not any(i.pattern_kind == "delegation_success_rate" for i in insights)


@pytest.mark.asyncio
class TestDetectDelegationPatternsRounds:
    """Rounds-needed p75 aggregation (ADR-0057 §D4)."""

    async def test_high_rounds_p75_produces_insight(self) -> None:
        """p75 ≥ 3 and count ≥ 10 → delegation_rounds insight."""
        engine = InsightsEngine()
        values = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 4, 4, 4, 4, 4]
        engine._queries.get_delegation_pattern_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "total": 20,
                "successes": 20,
                "rounds_needed_values": values,
                "missing_context_terms": [],
            }
        )
        insights = await engine.detect_delegation_patterns(days=30)
        rounds_insights = [i for i in insights if i.pattern_kind == "delegation_rounds"]
        assert len(rounds_insights) == 1
        ins = rounds_insights[0]
        assert int(ins.evidence["p75_rounds"]) >= 3
        assert 0.60 <= ins.confidence <= 0.85

    async def test_low_rounds_p75_skipped(self) -> None:
        """p75 = 2 (< 3) → no rounds insight."""
        engine = InsightsEngine()
        values = [1] * 10 + [2] * 4 + [2] * 6
        engine._queries.get_delegation_pattern_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "total": 20,
                "successes": 20,
                "rounds_needed_values": values,
                "missing_context_terms": [],
            }
        )
        insights = await engine.detect_delegation_patterns(days=30)
        assert not any(i.pattern_kind == "delegation_rounds" for i in insights)


@pytest.mark.asyncio
class TestDetectDelegationPatternsMissingContext:
    """Missing-context terms aggregation (ADR-0057 §D4)."""

    async def test_recurrent_missing_context_term_produces_insight(self) -> None:
        """Bucket count ≥ 3 → delegation_missing_context insight."""
        engine = InsightsEngine()
        engine._queries.get_delegation_pattern_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "total": 15,
                "successes": 15,
                "rounds_needed_values": [1] * 15,
                "missing_context_terms": [
                    ("no test harness provided", 5),
                    ("schema of orders table", 3),
                    ("rare-term", 1),
                ],
            }
        )
        insights = await engine.detect_delegation_patterns(days=30)
        missing = [i for i in insights if i.pattern_kind == "delegation_missing_context"]
        assert len(missing) == 2
        terms = [i.evidence["term"] for i in missing]
        assert "no test harness provided" in terms
        assert "schema of orders table" in terms
        assert 0.55 <= missing[0].confidence <= 0.95

    async def test_term_below_threshold_excluded(self) -> None:
        """Bucket count < 3 → term is NOT promoted to insight."""
        engine = InsightsEngine()
        engine._queries.get_delegation_pattern_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "total": 15,
                "successes": 15,
                "rounds_needed_values": [1] * 15,
                "missing_context_terms": [("rare-term", 2)],
            }
        )
        insights = await engine.detect_delegation_patterns(days=30)
        assert not any(i.pattern_kind == "delegation_missing_context" for i in insights)

    async def test_es_failure_returns_empty(self) -> None:
        """ES error → delegation_pattern_analysis_failed logged, returns []."""
        engine = InsightsEngine()
        engine._queries.get_delegation_pattern_buckets = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("ES unavailable")
        )
        insights = await engine.detect_delegation_patterns(days=30)
        assert insights == []
