"""Tests for delegation pattern analysis in InsightsEngine."""

import pytest

from personal_agent.insights.engine import InsightsEngine


class TestDetectDelegationPatterns:
    @pytest.fixture()
    def engine(self) -> InsightsEngine:
        """Create an InsightsEngine for testing.

        Uses the default constructor — ES unavailability is expected
        in unit tests and the method handles it gracefully.
        """
        return InsightsEngine()

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_data(
        self, engine: InsightsEngine
    ) -> None:
        """detect_delegation_patterns returns empty list when ES has no data."""
        insights = await engine.detect_delegation_patterns(days=30)
        assert insights == []

    @pytest.mark.asyncio
    async def test_accepts_custom_lookback(
        self, engine: InsightsEngine
    ) -> None:
        """Lookback parameter is accepted without error."""
        insights = await engine.detect_delegation_patterns(days=7)
        assert isinstance(insights, list)
