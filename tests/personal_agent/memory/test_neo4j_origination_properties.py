"""FRE-376 Phase 3 / ADR-0074 §I5: :Turn and :Entity nodes carry origination.

Asserts that the Cypher emitted by ``create_conversation`` and ``create_entity``
includes ``originating_trace_id`` / ``originating_session_id`` property writes
on the node, and that ``create_entity`` writes ``extractor_model`` as well.

Uses a mocked Neo4j driver (same pattern as
``test_create_conversation_user_id_propagation.py``) — runs in ``make test``.
"""

# ruff: noqa: D103

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from personal_agent.memory.models import Entity, TurnNode
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
async def test_create_conversation_writes_originating_identity_on_turn() -> None:
    service, _session, captured = _make_service_with_mock()

    turn = TurnNode(
        turn_id="trace-abc",
        trace_id="trace-abc",
        session_id="sess-xyz",
        timestamp=datetime.now(timezone.utc),
        user_message="hello",
    )

    ok = await service.create_conversation(turn)
    assert ok

    turn_cypher, turn_kwargs = captured[0]
    assert "MERGE (t:Turn {turn_id: $turn_id})" in turn_cypher
    assert "t.originating_trace_id = $originating_trace_id" in turn_cypher
    assert "t.originating_session_id = $originating_session_id" in turn_cypher
    assert turn_kwargs["originating_trace_id"] == "trace-abc"
    assert turn_kwargs["originating_session_id"] == "sess-xyz"


@pytest.mark.asyncio
async def test_create_entity_writes_originating_identity_and_extractor() -> None:
    service, _session, captured = _make_service_with_mock()

    entity = Entity(name="Acme Corp", entity_type="Organization")
    entity_id = await service.create_entity(
        entity,
        originating_trace_id="trace-abc",
        originating_session_id="sess-xyz",
        extractor_model="qwen3-8b",
    )
    assert entity_id == "Acme Corp"

    cypher, kwargs = captured[-1]
    assert "MERGE (e:Entity {name: $name})" in cypher
    assert "e.originating_trace_id = $originating_trace_id" in cypher
    assert "e.originating_session_id = $originating_session_id" in cypher
    assert "e.extractor_model = $extractor_model" in cypher
    assert kwargs["originating_trace_id"] == "trace-abc"
    assert kwargs["originating_session_id"] == "sess-xyz"
    assert kwargs["extractor_model"] == "qwen3-8b"


@pytest.mark.asyncio
async def test_create_conversation_inline_entity_carries_origination() -> None:
    """The inline (:Entity) MERGE inside create_conversation also writes origination."""
    service, _session, captured = _make_service_with_mock()

    turn = TurnNode(
        turn_id="trace-abc",
        trace_id="trace-abc",
        session_id="sess-xyz",
        timestamp=datetime.now(timezone.utc),
        user_message="hello",
        key_entities=["Acme"],
    )

    await service.create_conversation(turn)

    entity_calls = [c for c in captured if "MERGE (e:Entity" in c[0]]
    assert entity_calls, "Expected at least one :Entity MERGE call"
    cypher, kwargs = entity_calls[0]
    assert "e.originating_trace_id = $originating_trace_id" in cypher
    assert "e.originating_session_id = $originating_session_id" in cypher
    assert kwargs["originating_trace_id"] == "trace-abc"
    assert kwargs["originating_session_id"] == "sess-xyz"
