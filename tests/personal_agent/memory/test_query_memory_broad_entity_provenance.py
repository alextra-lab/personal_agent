"""FRE-1347: query_memory_broad's entity queries carry provenance_state/extractor_model/
source_referents, and the added OPTIONAL MATCH (e)-[:SOURCED_FROM]->(src:Source) doesn't
inflate the mention count via a cross product with the pre-existing Turn join.

Two independent risks a mocked test can pin: (1) the Cypher text itself must aggregate with
count(DISTINCT t) / collect(DISTINCT src.referent), not bare count(t) -- a regression here
wouldn't fail any assertion on returned *values* if a test only ever seeds one source per
entity, so the query text itself is asserted directly; (2) the returned dict must actually
carry the three new fields through to the caller.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.memory.service import MemoryService


def _make_service_capturing_entity_query(
    entity_rows: list[dict[str, object]],
) -> tuple[MemoryService, list[str]]:
    """MemoryService whose entity Cypher call returns entity_rows; captures every query text."""
    service = MemoryService.__new__(MemoryService)
    service.connected = True

    captured: list[str] = []

    empty_result = AsyncMock()
    empty_result.data = AsyncMock(return_value=[])
    empty_result.values = AsyncMock(return_value=[])

    entity_result = AsyncMock()
    entity_result.data = AsyncMock(return_value=entity_rows)

    mock_session = AsyncMock()

    async def _run_side_effect(cypher: str, **kwargs: object) -> AsyncMock:
        captured.append(cypher)
        if "MATCH (e:Entity)<-[:DISCUSSES]-(t:Turn)" in cypher:
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
    return service, captured


@pytest.mark.asyncio
async def test_plain_entity_query_aggregates_distinct_and_carries_new_fields() -> None:
    """No entity_types filter: the default (plain) Cypher block."""
    service, captured = _make_service_capturing_entity_query(
        [
            {
                "name": "SafeCart",
                "type": "Organization",
                "description": None,
                "mentions": 3,
                "provenance_state": "provenanced",
                "extractor_model": "qwen3-8b",
                "source_referents": ["https://a.example", "https://b.example"],
            }
        ]
    )

    result = await service.query_memory_broad(recency_days=90, limit=10)

    entity_cypher = next(c for c in captured if "MATCH (e:Entity)<-[:DISCUSSES]-(t:Turn)" in c)
    assert "count(DISTINCT t)" in entity_cypher
    assert "OPTIONAL MATCH (e)-[:SOURCED_FROM]->(src:Source)" in entity_cypher
    assert "collect(DISTINCT src.referent)" in entity_cypher

    assert result["entities"] == [
        {
            "name": "SafeCart",
            "type": "Organization",
            "description": None,
            "mentions": 3,
            "provenance_state": "provenanced",
            "extractor_model": "qwen3-8b",
            "source_referents": ["https://a.example", "https://b.example"],
        }
    ]


@pytest.mark.asyncio
async def test_entity_types_filtered_query_aggregates_distinct_and_carries_new_fields() -> None:
    """entity_types set: the second (type-filtered) Cypher block."""
    service, captured = _make_service_capturing_entity_query(
        [
            {
                "name": "Consolidated Widgets",
                "type": "Organization",
                "description": None,
                "mentions": 1,
                "provenance_state": "none",
                "extractor_model": "qwen3-8b",
                "source_referents": [],
            }
        ]
    )

    result = await service.query_memory_broad(
        entity_types=["Organization"], recency_days=90, limit=10
    )

    entity_cypher = next(c for c in captured if "MATCH (e:Entity)<-[:DISCUSSES]-(t:Turn)" in c)
    assert "e.entity_type IN $entity_types" in entity_cypher
    assert "count(DISTINCT t)" in entity_cypher
    assert "collect(DISTINCT src.referent)" in entity_cypher

    assert result["entities"][0]["provenance_state"] == "none"
    assert result["entities"][0]["source_referents"] == []
