"""FRE-673 acceptance: group memory recalls only under a verified identity.

Live Neo4j test (test substrate :7688). Seeds a ``visibility='group'`` Turn with a
unique entity and proves the chokepoint filter (FRE-229):

- ``query_memory(..., authenticated=True)``  → the seeded turn IS returned.
- ``query_memory(..., authenticated=False)`` → the seeded turn is filtered out.

This is the outcome-level proof that threading identity into the recall call
(FRE-673) is what reveals the agent's 100%-``group`` production memory. The
executor-side threading itself is guarded by the unit tests in
``tests/test_orchestrator/test_executor.py::TestExecutorRecallIdentityThreading``.

Marked ``integration`` so it stays out of the unit-only ``make test`` run.
Requires the test substrate (``make test-infra-up``) and ``AGENT_NEO4J_PASSWORD``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio

from personal_agent.memory.models import MemoryQuery, TurnNode
from personal_agent.memory.service import MemoryService

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def memory_service():
    """Create and connect to MemoryService against live Neo4j."""
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    connected = await service.connect()
    if not connected:
        pytest.skip("Neo4j not available (make test-infra-up)")

    yield service

    await service.disconnect()


@pytest.mark.asyncio
async def test_group_turn_recalled_only_when_authenticated(
    memory_service: MemoryService,
) -> None:
    """A group-visibility Turn is returned with authenticated=True and filtered without it."""
    uid = uuid4()
    entity = f"Fre673Entity{uuid4().hex[:10]}"  # unique → isolates this test's turn
    turn_id = f"turn-fre673-{uuid4()}"
    turn = TurnNode(
        turn_id=turn_id,
        timestamp=datetime.now(timezone.utc),
        user_message=f"Let us discuss {entity} in depth.",
        summary=f"Discussion about {entity}",
        key_entities=[entity],
    )
    written = await memory_service.create_conversation(turn, user_id=uid, visibility="group")
    assert written is True

    query = MemoryQuery(entity_names=[entity], limit=5)

    # Authenticated → group memory is revealed.
    auth_result = await memory_service.query_memory(query, user_id=uid, authenticated=True)
    assert any(c.turn_id == turn_id for c in auth_result.conversations), (
        "group-visibility turn must be returned to an authenticated query"
    )

    # Unauthenticated → the same group memory is filtered out (current/expected behaviour).
    unauth_result = await memory_service.query_memory(query, user_id=uid, authenticated=False)
    assert not any(c.turn_id == turn_id for c in unauth_result.conversations), (
        "group-visibility turn must be hidden from an unauthenticated query"
    )
