"""FRE-1347: query_memory's entity-match path resolves each matched entity's own terminus.

ADR-0098 Amendment A6. Before this fix, the entity-match path (MemoryService.query_memory,
driven by search_memory's entity_names/entity_types arguments) never populated
MemoryQueryResult.entities at all -- the recalled entity data available to the model was
Turn.key_entities, a list[str] property copied onto the Turn at write time with no link to
the :Entity node's own SOURCED_FROM provenance. That disconnect is the FRE-1338 shape this
ticket closes.

Uses a Cypher-text-dispatching mock (rather than positional call-count) so this test doesn't
depend on the exact number/order of session.run calls the surrounding recall logic makes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.memory.models import MemoryQuery
from personal_agent.memory.service import MemoryService


def _entity_node(
    name: str,
    *,
    provenance_state: str,
    extractor_model: str | None,
) -> MagicMock:
    """A raw Neo4j-node-shaped mock, matching what `RETURN e` yields."""
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


def _make_service(entity_rows: list[tuple[MagicMock, list[str]]]) -> MemoryService:
    """MemoryService whose entity-provenance query returns entity_rows.

    Every other session.run call returns an empty result, dispatched by inspecting the
    Cypher text.
    """
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    service._query_feedback_by_key = {}

    empty_result = AsyncMock()
    empty_result.values = AsyncMock(return_value=[])
    empty_result.data = AsyncMock(return_value=[])
    empty_result.single = AsyncMock(return_value=None)

    entity_provenance_result = AsyncMock()
    entity_provenance_result.values = AsyncMock(
        return_value=[[node, refs] for node, refs in entity_rows]
    )

    mock_session = AsyncMock()

    async def _run_side_effect(cypher: str, **kwargs: object) -> AsyncMock:
        if "SOURCED_FROM" in cypher and "MATCH (e:Entity)" in cypher:
            return entity_provenance_result
        return empty_result

    mock_session.run = AsyncMock(side_effect=_run_side_effect)

    service.driver = MagicMock()
    service.driver.session = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    return service


@pytest.mark.asyncio
async def test_provenanced_entity_resolved_with_source_referent() -> None:
    """AC-3: a fetched-page-backed entity comes back with its real referent, not a bare name."""
    node = _entity_node("SafeCart", provenance_state="provenanced", extractor_model="qwen3-8b")
    service = _make_service([(node, ["https://safecart.example/about"])])

    result = await service.query_memory(MemoryQuery(entity_names=["SafeCart"], limit=5))

    assert len(result.entities) == 1
    entity = result.entities[0]
    assert entity.name == "SafeCart"
    assert entity.provenance_state == "provenanced"
    assert entity.source_referents == ["https://safecart.example/about"]


@pytest.mark.asyncio
async def test_unprovenanced_entity_resolved_with_no_referent() -> None:
    """An agent-authored-terminus entity resolves with an empty referent list."""
    node = _entity_node("Consolidated Widgets", provenance_state="none", extractor_model="qwen3-8b")
    service = _make_service([(node, [])])

    result = await service.query_memory(MemoryQuery(entity_names=["Consolidated Widgets"], limit=5))

    assert len(result.entities) == 1
    entity = result.entities[0]
    assert entity.provenance_state == "none"
    assert entity.source_referents == []


@pytest.mark.asyncio
async def test_entity_provenance_query_resolves_by_entity_type_when_no_names_given() -> None:
    """entity_recall via entity_types alone (no entity_names) still resolves provenance.

    query_memory's new entity-provenance query picks between two mutually exclusive
    Cypher predicates/param sets depending on which of entity_names/entity_types the
    caller set; only the entity_names branch was covered before this test.
    """
    node = _entity_node("SafeCart", provenance_state="provenanced", extractor_model="qwen3-8b")
    service = _make_service([(node, ["https://safecart.example/about"])])

    result = await service.query_memory(MemoryQuery(entity_types=["Organization"], limit=5))

    assert len(result.entities) == 1
    assert result.entities[0].name == "SafeCart"
    assert result.entities[0].provenance_state == "provenanced"


@pytest.mark.asyncio
async def test_extractor_model_passes_through_verbatim_including_none() -> None:
    """The read side is a faithful passthrough of whatever the node property holds.

    None here stands in for a node with no extractor_model property at all (Neo4j has
    no persisted null -- the entitlement gate, not this read path, is what must not
    mistake that for the write side's USER_STATED_EXTRACTOR_SENTINEL; see
    grounding/test_search_memory_entitlement_e2e.py's seeded-negative test).
    """
    node = _entity_node("Owner Preference", provenance_state="none", extractor_model=None)
    service = _make_service([(node, [])])

    result = await service.query_memory(MemoryQuery(entity_names=["Owner Preference"], limit=5))

    assert len(result.entities) == 1
    assert result.entities[0].extractor_model is None


@pytest.mark.asyncio
async def test_non_entity_recall_does_not_resolve_entities() -> None:
    """A recall with no entity_names/entity_types never runs the entity-provenance query."""
    service = _make_service([])

    result = await service.query_memory(MemoryQuery(limit=5))

    assert result.entities == []


@pytest.mark.asyncio
async def test_entity_provenance_query_failure_degrades_to_empty_not_a_failed_recall() -> None:
    """A session.run failure on the new query must not fail the whole query_memory call.

    Proves the try/except scoped around just this query actually degrades gracefully
    (resolved_entities stays empty; conversations/relevance_scores still come back)
    rather than being verified only by reading the code.
    """
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    service._query_feedback_by_key = {}

    empty_result = AsyncMock()
    empty_result.values = AsyncMock(return_value=[])
    empty_result.data = AsyncMock(return_value=[])
    empty_result.single = AsyncMock(return_value=None)

    mock_session = AsyncMock()

    async def _run_side_effect(cypher: str, **kwargs: object) -> AsyncMock:
        if "SOURCED_FROM" in cypher and "MATCH (e:Entity)" in cypher:
            raise RuntimeError("Neo4j connection reset")
        return empty_result

    mock_session.run = AsyncMock(side_effect=_run_side_effect)

    service.driver = MagicMock()
    service.driver.session = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        )
    )

    result = await service.query_memory(MemoryQuery(entity_names=["SafeCart"], limit=5))

    assert result.entities == []
    assert result.conversations == []
