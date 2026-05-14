"""FRE-343: create_conversation MERGEs the PARTICIPATED_IN edge.

Verifies that the Cypher reaches the Neo4j driver — does not exercise
a live Neo4j (that's covered by test_participated_in_edge.py).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from personal_agent.memory.models import TurnNode
from personal_agent.memory.service import MemoryService


def _make_service_with_mock() -> tuple[MemoryService, AsyncMock]:
    """Build a MemoryService whose driver yields a mock async session."""
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    mock_driver = AsyncMock()
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_driver.session = lambda: mock_session  # not awaited
    service.driver = mock_driver
    return service, mock_session


@pytest.mark.asyncio
async def test_create_conversation_merges_participated_in_edge() -> None:
    """The PARTICIPATED_IN MERGE Cypher is issued after the Turn MERGE."""
    service, mock_session = _make_service_with_mock()

    captured_cypher: list[str] = []
    captured_kwargs: list[dict] = []

    async def capture_run(cypher: str, **kwargs: object):
        captured_cypher.append(cypher)
        captured_kwargs.append(dict(kwargs))
        return AsyncMock()

    mock_session.run = AsyncMock(side_effect=capture_run)

    uid = uuid4()
    turn = TurnNode(
        turn_id="turn-fre-343",
        timestamp=datetime.now(timezone.utc),
        user_message="hi",
    )

    await service.create_conversation(turn, user_id=uid, visibility="group")

    # Exactly one statement must be the PARTICIPATED_IN MERGE.
    participated = [c for c in captured_cypher if "PARTICIPATED_IN" in c]
    assert len(participated) == 1, f"expected one PARTICIPATED_IN MERGE, got {len(participated)}"

    cypher = participated[0]
    assert "MATCH (p:Person {user_id: $user_id})" in cypher
    assert "MATCH (t:Turn {turn_id: $turn_id})" in cypher
    assert "MERGE (p)-[r:PARTICIPATED_IN]->(t)" in cypher
    assert "ON CREATE SET r.created_at = $timestamp" in cypher

    # The user_id was passed through.
    idx = captured_cypher.index(cypher)
    assert captured_kwargs[idx].get("user_id") == str(uid)


@pytest.mark.asyncio
async def test_create_conversation_participated_in_after_turn_merge() -> None:
    """Order: Turn MERGE first, PARTICIPATED_IN second (before entity loop)."""
    service, mock_session = _make_service_with_mock()

    captured_cypher: list[str] = []

    async def capture_run(cypher: str, **kwargs: object):
        captured_cypher.append(cypher)
        return AsyncMock()

    mock_session.run = AsyncMock(side_effect=capture_run)

    turn = TurnNode(
        turn_id="turn-order",
        timestamp=datetime.now(timezone.utc),
        user_message="ping",
        key_entities=["Berlin"],
    )
    await service.create_conversation(turn, user_id=uuid4(), visibility="group")

    indices = {
        "turn_merge": next(i for i, c in enumerate(captured_cypher) if "MERGE (t:Turn {turn_id:" in c),
        "participated": next(i for i, c in enumerate(captured_cypher) if "PARTICIPATED_IN" in c),
        "entity_loop": next(i for i, c in enumerate(captured_cypher) if "DISCUSSES" in c),
    }
    assert indices["turn_merge"] < indices["participated"] < indices["entity_loop"]
