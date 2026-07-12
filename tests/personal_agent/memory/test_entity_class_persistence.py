"""FRE-864 / ADR-0115 D2: create_entity persists Entity.knowledge_class as e.class.

FRE-863 made the extractor emit a per-entity ``class`` (World/Personal), but the
write path dropped it: the ``Entity`` model had no field, and ``create_entity``'s
MERGE never set it. These tests prove the Cypher/param shape of the fix — the
live-substrate proof of the persisted *value* lives in
``test_entity_class_persistence_live.py``.

Mocked-driver pattern mirrors ``test_neo4j_origination_properties.py``.
"""

# ruff: noqa: D103

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from personal_agent.memory.models import Entity
from personal_agent.memory.service import MemoryService


def _make_service_with_mock() -> tuple[MemoryService, AsyncMock, list[tuple[str, dict]]]:
    """Build a MemoryService with a mock driver that captures every session.run call."""
    service = MemoryService.__new__(MemoryService)
    service.connected = True

    captured: list[tuple[str, dict]] = []

    async def capture_run(cypher: str, **kwargs: object):
        captured.append((cypher, dict(kwargs)))
        result = AsyncMock()
        result.single = AsyncMock(return_value={"entity_id": kwargs.get("name", "x")})
        return result

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.run = capture_run

    mock_driver = AsyncMock()
    mock_driver.session = lambda: mock_session
    service.driver = mock_driver
    return service, mock_session, captured


@pytest.mark.asyncio
async def test_create_entity_writes_class_first_write_wins_case() -> None:
    """A Personal-classed entity's MERGE carries a first-write-wins e.class clause."""
    service, _session, captured = _make_service_with_mock()

    entity = Entity(name="Dr. Chen", entity_type="Person", knowledge_class="Personal")
    await service.create_entity(entity)

    cypher, kwargs = captured[-1]
    assert (
        "e.class = CASE WHEN e.class IS NULL OR e.class = '' THEN $class ELSE e.class END" in cypher
    )
    assert kwargs["class"] == "Personal"


@pytest.mark.asyncio
async def test_create_entity_writes_world_class() -> None:
    """A World-classed entity's MERGE carries class=World."""
    service, _session, captured = _make_service_with_mock()

    entity = Entity(name="GraphRAG", entity_type="MethodOrConcept", knowledge_class="World")
    await service.create_entity(entity)

    _cypher, kwargs = captured[-1]
    assert kwargs["class"] == "World"


@pytest.mark.asyncio
async def test_create_entity_omits_class_clause_when_unset() -> None:
    """A caller that never sets knowledge_class (e.g. gateway store_fact) writes no class param.

    Parity with the coordinates/geocoded conditional-append pattern — no stray
    ``class=None`` write for non-extraction callers.
    """
    service, _session, captured = _make_service_with_mock()

    entity = Entity(name="Acme Corp", entity_type="Organization")
    await service.create_entity(entity)

    cypher, kwargs = captured[-1]
    assert "e.class" not in cypher
    assert "class" not in kwargs
