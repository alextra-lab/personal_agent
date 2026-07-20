"""ADR-0121 T4 / AC-8 — spend attributable to the model that incurred it.

AC-8: after two turns on two different selected primary models, querying
spend grouped by model returns non-zero, correctly-split values. These tests
prove ``get_cost_by_model`` at the query layer, mirroring the
``test_cost_tracker_identity.py`` mock-pool pattern (no live Postgres needed).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.llm_client.cost_tracker import CostTrackerService


def _tracker_with_mock_rows(rows: list[dict[str, object]]) -> CostTrackerService:
    """Return a tracker wired to a mock pool whose ``fetch`` returns ``rows``."""
    tracker = CostTrackerService()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    tracker.pool = pool  # type: ignore[assignment]
    return tracker


@pytest.mark.asyncio
async def test_get_cost_by_model_splits_two_distinct_models_correctly() -> None:
    """Two turns on two different models return non-zero, correctly-split spend.

    Fixture values are distinct on purpose (AC-8 fixture-collision guard, same
    principle as ADR-0121 AC-6): no assertion could pass by coincidence.
    """
    rows = [
        {"model": "anthropic/claude-sonnet-4-6", "cost": 0.045},
        {"model": "anthropic/claude-haiku-4-5-20251001", "cost": 0.012},
    ]
    tracker = _tracker_with_mock_rows(rows)

    result = await tracker.get_cost_by_model(days=7)

    assert result == {
        "anthropic/claude-sonnet-4-6": 0.045,
        "anthropic/claude-haiku-4-5-20251001": 0.012,
    }
    assert all(v > 0 for v in result.values()), "spend must be non-zero for both models"
    assert len(result) == 2, "the two models must not collapse into one bucket"


@pytest.mark.asyncio
async def test_get_cost_by_model_returns_empty_dict_when_pool_unavailable() -> None:
    """No live pool -> empty dict, never a crash (matches get_cost_by_purpose)."""
    tracker = CostTrackerService()
    assert tracker.pool is None

    result = await tracker.get_cost_by_model()

    assert result == {}
