"""Live-Neo4j behavioural proof of FRE-864 (ADR-0115 D2 — Entity class persistence).

Marked ``integration`` (out of ``make test``); runs against the isolated test Neo4j
(:7688). ``generate_embedding`` is patched to a zero vector so the dedup path is
skipped and each test drives ``create_entity`` deterministically.

AC-1 — a Personal fixture persists class=Personal; a World fixture persists
class=World. AC-4 — an entity written with the fail-open default (class=World)
exists in Core, not dropped. A first-write-wins regression proves class isn't
silently overwritten, matching entity_type/properties. Also proves
``ensure_entity_class_index()`` succeeds against the live substrate.
"""

from __future__ import annotations

# ruff: noqa: D103
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from personal_agent.memory.models import Entity
from personal_agent.memory.service import MemoryService

pytestmark = pytest.mark.integration

_ZERO_EMBED = patch(
    "personal_agent.memory.service.generate_embedding",
    new=AsyncMock(return_value=[0.0, 0.0]),
)


@pytest_asyncio.fixture
async def svc():
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    if not await service.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")
    assert service.driver is not None
    async with service.driver.session() as s:
        await s.run("MATCH (e:Entity) WHERE e.name STARTS WITH 'FRE864_' DETACH DELETE e")
    yield service
    async with service.driver.session() as s:
        await s.run("MATCH (e:Entity) WHERE e.name STARTS WITH 'FRE864_' DETACH DELETE e")
    await service.disconnect()


async def _class_of(service: MemoryService, name: str) -> str | None:
    assert service.driver is not None
    async with service.driver.session() as s:
        r = await s.run("MATCH (e:Entity {name: $n}) RETURN e.class AS c", n=name)
        rec = await r.single()
        return rec["c"] if rec else None


@pytest.mark.asyncio
async def test_ac1_personal_fixture_persists_class_personal(svc: MemoryService) -> None:
    entity = Entity(name="FRE864_Chen", entity_type="Person", knowledge_class="Personal")
    with _ZERO_EMBED:
        entity_id = await svc.create_entity(entity)
    assert entity_id == "FRE864_Chen"
    assert await _class_of(svc, "FRE864_Chen") == "Personal"


@pytest.mark.asyncio
async def test_ac1_world_fixture_persists_class_world(svc: MemoryService) -> None:
    entity = Entity(name="FRE864_GraphRAG", entity_type="MethodOrConcept", knowledge_class="World")
    with _ZERO_EMBED:
        entity_id = await svc.create_entity(entity)
    assert entity_id == "FRE864_GraphRAG"
    assert await _class_of(svc, "FRE864_GraphRAG") == "World"


@pytest.mark.asyncio
async def test_ac4_fail_open_item_persists_not_dropped(svc: MemoryService) -> None:
    """An item the classifier fails open on (class=World) still lands in Core."""
    entity = Entity(name="FRE864_Uncertain", entity_type="MethodOrConcept", knowledge_class="World")
    with _ZERO_EMBED:
        entity_id = await svc.create_entity(entity)
    assert entity_id == "FRE864_Uncertain"
    assert await _class_of(svc, "FRE864_Uncertain") == "World"


@pytest.mark.asyncio
async def test_class_is_first_write_wins(svc: MemoryService) -> None:
    """A later write with a different class does not overwrite the first.

    Parity with entity_type/properties (FRE-375).
    """
    with _ZERO_EMBED:
        await svc.create_entity(
            Entity(name="FRE864_Stable", entity_type="Person", knowledge_class="Personal")
        )
        await svc.create_entity(
            Entity(name="FRE864_Stable", entity_type="Person", knowledge_class="World")
        )
    assert await _class_of(svc, "FRE864_Stable") == "Personal"


@pytest.mark.asyncio
async def test_ensure_entity_class_index_succeeds(svc: MemoryService) -> None:
    assert await svc.ensure_entity_class_index() is True
