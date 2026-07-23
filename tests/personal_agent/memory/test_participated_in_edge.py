"""Live Neo4j test for the PARTICIPATED_IN provenance edge (FRE-343).

Requires ``make up`` infra (Neo4j running at 127.0.0.1:7687).
Marked ``integration`` so it stays out of the unit-only ``make test`` run.

Fixture pattern mirrors tests/test_memory/test_memory_service.py:
- ``memory_service`` — connected MemoryService (skips if Neo4j unavailable)
- ``clean_test_data`` — thin yield wrapper for symmetry; no teardown needed
  because every test uses unique turn_ids so state is isolated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from personal_agent.memory.models import TurnNode
from personal_agent.memory.service import MemoryService

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures — mirror tests/test_memory/test_memory_service.py style
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def memory_service():
    """Create and connect to MemoryService against live Neo4j."""
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    connected = await service.connect()
    if not connected:
        pytest.skip("Neo4j not available (make up)")

    yield service

    await service.disconnect()


@pytest_asyncio.fixture
async def clean_test_data(memory_service: MemoryService):
    """Yield; each test uses unique IDs so no teardown is required."""
    yield


@pytest_asyncio.fixture
async def seeded_user(memory_service: MemoryService, clean_test_data: None) -> UUID:
    """Provision a :Person node for a test user and return its UUID."""
    uid = uuid4()
    await memory_service.get_or_provision_user_person(
        user_id=uid,
        email=f"test-{uid}@example.com",
        display_name="Test User",
    )
    return uid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_participated_in_edge_is_created(
    memory_service: MemoryService, seeded_user: UUID
) -> None:
    """create_conversation writes a (:Person)-[:PARTICIPATED_IN]->(:Turn) edge."""
    turn = TurnNode(
        turn_id=f"turn-{uuid4()}",
        timestamp=datetime.now(timezone.utc),
        user_message="hello world",
    )
    ok = await memory_service.create_conversation(turn, user_id=seeded_user, visibility="group")
    assert ok is True

    async with memory_service.driver.session() as session:
        result = await session.run(
            """
            MATCH (p:Person {user_id: $uid})-[r:PARTICIPATED_IN]->(t:Turn {turn_id: $tid})
            RETURN r.created_at AS created_at
            """,
            uid=str(seeded_user),
            tid=turn.turn_id,
        )
        record = await result.single()

    assert record is not None, "PARTICIPATED_IN edge was not created"
    assert record["created_at"] == turn.timestamp.isoformat()


@pytest.mark.asyncio
async def test_participated_in_edge_is_idempotent(
    memory_service: MemoryService, seeded_user: UUID
) -> None:
    """Calling create_conversation twice produces exactly one PARTICIPATED_IN edge."""
    turn = TurnNode(
        turn_id=f"turn-{uuid4()}",
        timestamp=datetime.now(timezone.utc),
        user_message="hello again",
    )
    await memory_service.create_conversation(turn, user_id=seeded_user, visibility="group")
    await memory_service.create_conversation(turn, user_id=seeded_user, visibility="group")

    async with memory_service.driver.session() as session:
        result = await session.run(
            """
            MATCH (p:Person {user_id: $uid})-[r:PARTICIPATED_IN]->(t:Turn {turn_id: $tid})
            RETURN count(r) AS cnt
            """,
            uid=str(seeded_user),
            tid=turn.turn_id,
        )
        record = await result.single()

    assert record is not None
    assert record["cnt"] == 1, f"Expected 1 edge (MERGE idempotency), got {record['cnt']}"


@pytest.mark.asyncio
async def test_participated_in_skipped_when_person_missing(
    memory_service: MemoryService, clean_test_data: None
) -> None:
    """MATCH on non-existent :Person writes no edge; Turn node is still created."""
    bogus_uid = uuid4()  # never provisioned — no :Person node exists
    turn = TurnNode(
        turn_id=f"turn-{uuid4()}",
        timestamp=datetime.now(timezone.utc),
        user_message="orphan",
    )
    ok = await memory_service.create_conversation(turn, user_id=bogus_uid, visibility="group")
    assert ok is True, "create_conversation should succeed even if :Person is absent"

    async with memory_service.driver.session() as session:
        # No PARTICIPATED_IN edge should exist for this turn.
        result = await session.run(
            """
            MATCH ()-[r:PARTICIPATED_IN]->(t:Turn {turn_id: $tid})
            RETURN count(r) AS cnt
            """,
            tid=turn.turn_id,
        )
        record = await result.single()

    assert record is not None
    assert record["cnt"] == 0, (
        f"Expected 0 edges when :Person is absent, got {record['cnt']}"
    )
