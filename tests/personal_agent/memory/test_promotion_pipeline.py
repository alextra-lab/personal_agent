"""CP-26 / memory-quality diagnostic tests for the promotion pipeline.

Full eval CP-26 requires end-to-end extraction → graph write → promotion. This module
tests the promotion stage in isolation so regressions in `run_promotion_pipeline` are
caught; wiring from consolidator/captures is covered elsewhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.memory.fact import PromotionCandidate
from personal_agent.memory.promote import run_promotion_pipeline


@pytest.mark.asyncio
async def test_cp26_entity_lifecycle_promotion_when_candidates_exist() -> None:
    """EVAL-08 CP-26: multi-entity session → promotion accepts all when Neo4j promotes.

    Simulates the *promotion* half: consolidator/entity extraction must supply
    `PromotionCandidate`s with these names; this test ensures the pipeline calls
    `promote_entity` for each and counts successes.
    """
    mock_service = MagicMock()
    mock_service.promote_entity = AsyncMock(return_value=True)

    names = ("DataForge", "Apache Flink", "ClickHouse", "Priya Sharma")
    candidates = [
        PromotionCandidate(
            entity_name=name,
            entity_type="Mixed",
            mention_count=3,
            first_seen=datetime(2026, 3, 1, tzinfo=timezone.utc),
            last_seen=datetime(2026, 3, 30, tzinfo=timezone.utc),
            source_turn_ids=[f"turn-{i}"],
            description=None,
        )
        for i, name in enumerate(names)
    ]

    result = await run_promotion_pipeline(
        service=mock_service,
        candidates=candidates,
        trace_id="cp26-sim",
    )
    assert result.promoted_count == len(names)
    assert mock_service.promote_entity.await_count == len(names)
