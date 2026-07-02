"""Integration tests for the closed-axis structural recall arm (FRE-707, ADR-0104 AC-4).

These require the test Neo4j substrate (``make test-infra-up``, :7688) and skip
cleanly when it is unavailable. They prove the arm's behaviour end to end against
real Cypher:

* AC-4a — the arm is gated off by default and contributes nothing.
* AC-4b — an enabled type predicate does not silently drop ``""``/``"Unknown"`` rows.
* AC-4c — the open axis (free-text name) is never used as a hard filter.
* FRE-229 — the co-occurrence traversal never surfaces an entity reached only
  through a Turn the caller cannot see.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from personal_agent.config.settings import get_settings
from personal_agent.memory.service import MemoryService


@pytest_asyncio.fixture
async def memory_service():
    """Connect to the test Neo4j substrate; skip if unavailable."""
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    connected = await service.connect()
    if not connected:
        pytest.skip("Neo4j not available (make test-infra-up)")
    yield service
    await service.disconnect()


async def _seed_entity(
    service: MemoryService,
    *,
    name: str,
    entity_type: str,
    last_seen: str,
    visibility: str = "public",
) -> None:
    """MERGE an Entity node with exact type/recency/visibility control."""
    async with service.driver.session() as session:
        await session.run(
            """
            MERGE (e:Entity {name: $name})
            SET e.entity_type = $entity_type,
                e.last_seen = $last_seen,
                e.first_seen = $last_seen,
                e.mention_count = 1,
                e.visibility = $visibility
            """,
            name=name,
            entity_type=entity_type,
            last_seen=last_seen,
            visibility=visibility,
        )


async def _seed_cooccurrence(
    service: MemoryService,
    *,
    anchor: str,
    neighbour: str,
    turn_visibility: str,
    last_seen: str,
) -> None:
    """Seed anchor and neighbour entities that co-occur via a single Turn."""
    turn_id = f"turn-{uuid.uuid4()}"
    await _seed_entity(service, name=anchor, entity_type="Person", last_seen=last_seen)
    await _seed_entity(service, name=neighbour, entity_type="Person", last_seen=last_seen)
    async with service.driver.session() as session:
        await session.run(
            """
            MERGE (t:Turn {turn_id: $turn_id})
            SET t.visibility = $turn_visibility, t.timestamp = $last_seen
            WITH t
            MATCH (a:Entity {name: $anchor})
            MATCH (n:Entity {name: $neighbour})
            MERGE (t)-[:DISCUSSES]->(a)
            MERGE (t)-[:DISCUSSES]->(n)
            """,
            turn_id=turn_id,
            turn_visibility=turn_visibility,
            anchor=anchor,
            neighbour=neighbour,
            last_seen=last_seen,
        )


async def _purge(service: MemoryService, prefix: str) -> None:
    """Best-effort removal of nodes seeded under a unique prefix."""
    async with service.driver.session() as session:
        await session.run(
            "MATCH (n) WHERE n.name STARTS WITH $p OR n.turn_id STARTS WITH $p DETACH DELETE n",
            p=prefix,
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _old_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()


@pytest.mark.asyncio
async def test_arm_gated_off_returns_empty(memory_service, monkeypatch):
    """AC-4a / flag-dark: the arm returns nothing while the master flag is off."""
    prefix = f"fre707-{uuid.uuid4()}"
    await _seed_entity(
        memory_service, name=f"{prefix}-A", entity_type="Person", last_seen=_now_iso()
    )
    monkeypatch.setattr(get_settings(), "structural_arm_enabled", False, raising=False)
    try:
        result = await memory_service.structural_recall_arm(entity_types=["Person"])
        assert result == []
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.asyncio
async def test_type_scoped_recall_keeps_unenforced_entities(memory_service, monkeypatch):
    """AC-4b: a type predicate keeps ``""`` and ``"Unknown"`` rows but still narrows."""
    prefix = f"fre707-{uuid.uuid4()}"
    now = _now_iso()
    person = f"{prefix}-person"
    empty = f"{prefix}-empty"
    unknown = f"{prefix}-unknown"
    location = f"{prefix}-location"
    await _seed_entity(memory_service, name=person, entity_type="Person", last_seen=now)
    await _seed_entity(memory_service, name=empty, entity_type="", last_seen=now)
    await _seed_entity(memory_service, name=unknown, entity_type="Unknown", last_seen=now)
    await _seed_entity(memory_service, name=location, entity_type="Location", last_seen=now)

    monkeypatch.setattr(get_settings(), "structural_arm_enabled", True, raising=False)
    monkeypatch.setattr(get_settings(), "structural_type_predicate_enabled", True, raising=False)
    try:
        result = await memory_service.structural_recall_arm(entity_types=["Person"])
        names = {e.name for e in result}
        # The requested type AND both unenforced-type rows survive (AC-4b).
        assert person in names
        assert empty in names
        assert unknown in names
        # The predicate still narrows: an entity with a *different* enforced type
        # is excluded (proves it is not a no-op).
        assert location not in names
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.asyncio
async def test_open_axis_not_filtered(memory_service, monkeypatch):
    """AC-4c: two same-type/recency entities with different names both return."""
    prefix = f"fre707-{uuid.uuid4()}"
    now = _now_iso()
    a = f"{prefix}-alpha"
    b = f"{prefix}-beta"
    await _seed_entity(memory_service, name=a, entity_type="Person", last_seen=now)
    await _seed_entity(memory_service, name=b, entity_type="Person", last_seen=now)

    monkeypatch.setattr(get_settings(), "structural_arm_enabled", True, raising=False)
    try:
        # No type predicate, no anchors: a plain closed-axis scan must not filter
        # on the free-text name axis.
        result = await memory_service.structural_recall_arm(recency_days=30)
        names = {e.name for e in result}
        assert a in names
        assert b in names
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.asyncio
async def test_recency_window_filters(memory_service, monkeypatch):
    """Closed-axis recency: an entity outside the window is excluded."""
    prefix = f"fre707-{uuid.uuid4()}"
    recent = f"{prefix}-recent"
    stale = f"{prefix}-stale"
    await _seed_entity(memory_service, name=recent, entity_type="Person", last_seen=_now_iso())
    await _seed_entity(memory_service, name=stale, entity_type="Person", last_seen=_old_iso())

    monkeypatch.setattr(get_settings(), "structural_arm_enabled", True, raising=False)
    try:
        result = await memory_service.structural_recall_arm(recency_days=30)
        names = {e.name for e in result}
        assert recent in names
        assert stale not in names
    finally:
        await _purge(memory_service, prefix)


@pytest.mark.asyncio
async def test_traversal_excludes_private_turn(memory_service, monkeypatch):
    """FRE-229: co-occurrence via a private Turn does not leak the neighbour."""
    prefix = f"fre707-{uuid.uuid4()}"
    now = _now_iso()
    anchor = f"{prefix}-anchor"
    private_neighbour = f"{prefix}-private-neighbour"
    public_neighbour = f"{prefix}-public-neighbour"
    other_user = str(uuid.uuid4())

    # anchor co-occurs with private_neighbour ONLY through a private turn,
    # and with public_neighbour through a public turn (positive control).
    await _seed_cooccurrence(
        memory_service,
        anchor=anchor,
        neighbour=private_neighbour,
        turn_visibility=f"private:{other_user}",
        last_seen=now,
    )
    await _seed_cooccurrence(
        memory_service,
        anchor=anchor,
        neighbour=public_neighbour,
        turn_visibility="public",
        last_seen=now,
    )

    monkeypatch.setattr(get_settings(), "structural_arm_enabled", True, raising=False)
    try:
        # Unauthenticated caller: the private turn is invisible.
        result = await memory_service.structural_recall_arm(
            anchor_names=[anchor], authenticated=False
        )
        names = {e.name for e in result}
        # Positive control: the public co-occurrence surfaces.
        assert public_neighbour in names
        # The leak guard: the private-turn co-occurrence does not.
        assert private_neighbour not in names
    finally:
        await _purge(memory_service, prefix)
