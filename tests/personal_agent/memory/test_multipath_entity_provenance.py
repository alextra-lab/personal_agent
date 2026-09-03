"""FRE-1347 master-gate bounce: the multipath recall path must carry entitlement fields too.

The first PR deferred `_multipath_broad_entities`/`_resolve_fused_turns` as "dormant" because
`multipath_recall_enabled` defaults False. That premise was false in production: `.env` sets
`AGENT_MULTIPATH_RECALL_ENABLED=true`, and `query_memory` (`memory/service.py`) early-returns
into `_multipath_query_memory` whenever `query_text` is set and the flag is on -- which
`search_memory_executor` always does. So the production entity-match path was the multipath one,
not the legacy path this ticket's first pass fixed, and it read every entity as `AGENT_DERIVED`
regardless of real provenance.

These tests set `multipath_recall_enabled = True` and drive `query_memory`/`query_memory_broad`
for real, patching only `_multipath_fused_recall` (the arms/RRF/rerank machinery, exercised by
its own tests elsewhere) so the entity-resolution Cypher this bug lives in runs against a mocked
driver, unpatched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.config.settings import get_settings
from personal_agent.memory.fusion import FusedResult, MultiPathRecallResult
from personal_agent.memory.models import MemoryQuery
from personal_agent.memory.service import MemoryService


def _entity_node(name: str, *, provenance_state: str, extractor_model: str | None) -> MagicMock:
    node = MagicMock()
    node.get = lambda k, default=None: {
        "name": name,
        "entity_type": "Organization",
        "description": None,
        "mention_count": 1,
        "provenance_state": provenance_state,
        "extractor_model": extractor_model,
        "properties": "{}",
    }.get(k, default)
    return node


@pytest.fixture(autouse=True)
def _multipath_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "multipath_recall_enabled", True, raising=False)


@pytest.mark.asyncio
async def test_query_memory_multipath_entity_carries_real_provenance() -> None:
    """query_memory, flag ON: the fused-entity path resolves real provenance, not the default."""
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    service._query_feedback_by_key = {}

    fused = MultiPathRecallResult(
        items=[FusedResult(item_id="elem-1", score=1.0, arm_count=1, kind="entity")],
        arms_executed=["dense"],
        arms_failed=[],
        per_arm_counts={"dense": 1},
        fused_set_size=1,
        path="entity",
    )
    service._multipath_fused_recall = AsyncMock(return_value=fused)

    node = _entity_node("SafeCart", provenance_state="provenanced", extractor_model="qwen3-8b")
    entity_result = AsyncMock()
    entity_result.values = AsyncMock(
        return_value=[["elem-1", node, ["https://safecart.example/about"]]]
    )
    empty_result = AsyncMock()
    empty_result.values = AsyncMock(return_value=[])
    empty_result.data = AsyncMock(return_value=[])
    empty_result.single = AsyncMock(return_value=None)

    mock_session = AsyncMock()

    async def _run_side_effect(cypher: str, **kwargs: object) -> AsyncMock:
        if "SOURCED_FROM" in cypher and "elementId(e) = eid" in cypher:
            return entity_result
        return empty_result

    mock_session.run = AsyncMock(side_effect=_run_side_effect)

    service.driver = MagicMock()
    service.driver.session = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        )
    )

    result = await service.query_memory(
        MemoryQuery(entity_names=["SafeCart"], limit=5), query_text="SafeCart"
    )

    assert len(result.entities) == 1
    entity = result.entities[0]
    assert entity.provenance_state == "provenanced"
    assert entity.source_referents == ["https://safecart.example/about"]
    assert entity.extractor_model == "qwen3-8b"


@pytest.mark.asyncio
async def test_query_memory_broad_multipath_entity_carries_real_provenance() -> None:
    """query_memory_broad, flag ON: _multipath_broad_entities threads the same fields."""
    service = MemoryService.__new__(MemoryService)
    service.connected = True

    fused = MultiPathRecallResult(
        items=[FusedResult(item_id="elem-1", score=1.0, arm_count=1, kind="entity")],
        arms_executed=["dense"],
        arms_failed=[],
        per_arm_counts={"dense": 1},
        fused_set_size=1,
        path="broad",
    )
    service._multipath_fused_recall = AsyncMock(return_value=fused)

    entity_result = AsyncMock()
    entity_result.data = AsyncMock(
        return_value=[
            {
                "id": "elem-1",
                "name": "SafeCart",
                "type": "Organization",
                "description": None,
                "mentions": 2,
                "provenance_state": "provenanced",
                "extractor_model": "qwen3-8b",
                "source_referents": ["https://safecart.example/about"],
            }
        ]
    )
    empty_result = AsyncMock()
    empty_result.values = AsyncMock(return_value=[])
    empty_result.data = AsyncMock(return_value=[])
    empty_result.single = AsyncMock(return_value=None)

    mock_session = AsyncMock()

    async def _run_side_effect(cypher: str, **kwargs: object) -> AsyncMock:
        if "SOURCED_FROM" in cypher and "elementId(e) = eid" in cypher:
            return entity_result
        return empty_result

    mock_session.run = AsyncMock(side_effect=_run_side_effect)

    service.driver = MagicMock()
    service.driver.session = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        )
    )

    result = await service.query_memory_broad(query_text="SafeCart", recency_days=90, limit=10)

    assert len(result["entities"]) == 1
    entity = result["entities"][0]
    assert entity["provenance_state"] == "provenanced"
    assert entity["source_referents"] == ["https://safecart.example/about"]
    assert entity["extractor_model"] == "qwen3-8b"
