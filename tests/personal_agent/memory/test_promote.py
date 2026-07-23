"""Tests for the promote() pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.memory.fact import PromotionCandidate, PromotionResult
from personal_agent.memory.promote import run_promotion_pipeline


class TestRunPromotionPipeline:
    @pytest.mark.asyncio
    async def test_promotes_qualifying_entities(self) -> None:
        mock_service = MagicMock()
        mock_service.promote_entity = AsyncMock(return_value=True)

        candidates = [
            PromotionCandidate(
                entity_name="Neo4j", entity_type="Technology",
                mention_count=20,
                first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
                last_seen=datetime(2026, 3, 15, tzinfo=timezone.utc),
                source_turn_ids=["t1", "t2"], description="Graph database",
            ),
        ]

        result = await run_promotion_pipeline(
            service=mock_service, candidates=candidates, trace_id="test",
        )
        assert result.promoted_count == 1
        assert result.success is True

    @pytest.mark.asyncio
    async def test_handles_promote_failure(self) -> None:
        mock_service = MagicMock()
        mock_service.promote_entity = AsyncMock(return_value=False)

        candidates = [
            PromotionCandidate(
                entity_name="Missing", entity_type="Unknown",
                mention_count=10,
                first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
                last_seen=datetime(2026, 3, 1, tzinfo=timezone.utc),
                source_turn_ids=["t1"], description=None,
            ),
        ]

        result = await run_promotion_pipeline(
            service=mock_service, candidates=candidates, trace_id="test",
        )
        assert result.promoted_count == 0
        assert result.success is False
        assert len(result.errors) == 1
