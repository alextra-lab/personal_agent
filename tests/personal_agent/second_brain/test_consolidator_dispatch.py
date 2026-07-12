"""FRE-728: ADR-0115 D3 dispatch — route extracted entities by output_kind.

Only `knowledge` items may reach Core. `ephemeral` items are dropped (already
observed in Elasticsearch via the existing capture-time write, independent of
consolidation). `finding` items route to `sysgraph`. These tests mock
``MemoryService`` and the sysgraph singleton so the routing logic is proven
without a live Neo4j/Postgres stack.

The MERGE-leak regression test is the important one: ``create_conversation``
is called *before* the entity-creation loop and MERGEs a bare ``:Entity`` node
for every name in ``TurnNode.key_entities`` (``memory/service.py``) — so
``key_entities`` itself must already exclude ephemeral/finding names, or
gating only ``create_entity`` would still leak them into Core.
"""

# ruff: noqa: D103

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.second_brain.consolidator import SecondBrainConsolidator

_TURN_TS = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def _make_capture() -> TaskCapture:
    return TaskCapture(
        trace_id=str(uuid.uuid4()),
        timestamp=_TURN_TS,
        user_message="I'm leasing a Rafale -- also is your KG healthy?",
        assistant_response="Noted, and yes.",
        session_id=str(uuid.uuid4()),
        tools_used=[],
        duration_ms=100,
        outcome="completed",
        user_id=uuid.uuid4(),
        eval_mode=False,
    )


def _knowledge_entity(name: str = "Rafale") -> dict[str, Any]:
    return {
        "name": name,
        "type": "Vehicle",
        "class": "World",
        "description": "A fighter jet the owner is leasing (fictional framing).",
        "output_kind": "knowledge",
        "description_update_kind": "new",
    }


def _ephemeral_entity(name: str = "Elasticsearch") -> dict[str, Any]:
    return {
        "name": name,
        "type": "TechnicalArtifact",
        "description": "status yellow",
        "output_kind": "ephemeral",
    }


def _finding_entity(name: str = "Postgres") -> dict[str, Any]:
    return {
        "name": name,
        "type": "TechnicalArtifact",
        "description": "no connection pooling -- reaper exhausts connections",
        "output_kind": "finding",
    }


def _extraction(
    entities: list[dict[str, Any]], relationships: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "entities": entities,
        "relationships": relationships or [],
        "entity_names": [e.get("name", "") for e in entities if e.get("name")],
        "summary": "mixed turn",
        "stances": [],
        "claims": [],
    }


@pytest.fixture
def memory_service() -> MagicMock:
    svc = MagicMock()
    svc.create_conversation = AsyncMock(return_value=None)
    svc.create_entity = AsyncMock(return_value="entity-id-1")
    svc.create_relationship = AsyncMock(return_value="rel-1")
    svc.fetch_turn_discusses_relationship_element_ids = AsyncMock(return_value=[])
    svc.assert_stance = AsyncMock(return_value=True)
    svc.assert_claim = AsyncMock(return_value="claim-id-1")
    return svc


@pytest.fixture
def consolidator(memory_service: MagicMock) -> SecondBrainConsolidator:
    return SecondBrainConsolidator(memory_service=memory_service)


def _patches(extraction: dict[str, Any]):
    return (
        patch(
            "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
            new_callable=AsyncMock,
            return_value=extraction,
        ),
        patch(
            "personal_agent.second_brain.consolidator.previous_attempt_count",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch(
            "personal_agent.second_brain.consolidator.record_consolidation_attempt",
            new_callable=AsyncMock,
        ),
    )


@pytest.mark.asyncio
async def test_ephemeral_entity_not_written_to_core(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    capture = _make_capture()
    extraction = _extraction([_ephemeral_entity()])
    p1, p2, p3 = _patches(extraction)
    with (
        p1,
        p2,
        p3,
        patch(
            "personal_agent.second_brain.consolidator.get_default_sysgraph_repo",
            return_value=None,
        ),
    ):
        result = await consolidator._process_capture(capture)

    memory_service.create_entity.assert_not_awaited()
    assert result["entities_created"] == 0
    assert result["entities_dispatched_ephemeral"] == 1
    # Not leaked via the Turn's key_entities either (the MERGE-leak path).
    turn_arg = memory_service.create_conversation.await_args.args[0]
    assert turn_arg.key_entities == []


@pytest.mark.asyncio
async def test_finding_entity_routed_to_sysgraph_not_core(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    capture = _make_capture()
    extraction = _extraction([_finding_entity()])
    mock_repo = MagicMock()
    mock_repo.record_finding = AsyncMock(return_value=None)
    p1, p2, p3 = _patches(extraction)
    with (
        p1,
        p2,
        p3,
        patch(
            "personal_agent.second_brain.consolidator.get_default_sysgraph_repo",
            return_value=mock_repo,
        ),
    ):
        result = await consolidator._process_capture(capture)

    memory_service.create_entity.assert_not_awaited()
    mock_repo.record_finding.assert_awaited_once()
    call_kwargs = mock_repo.record_finding.await_args.kwargs
    assert call_kwargs["entity_name"] == "Postgres"
    assert call_kwargs["entity_type"] == "TechnicalArtifact"
    assert call_kwargs["trace_id"] == capture.trace_id
    assert call_kwargs["session_id"] == capture.session_id
    assert result["entities_created"] == 0
    assert result["entities_dispatched_finding"] == 1
    assert result["entities_dispatch_finding_failed"] == 0
    turn_arg = memory_service.create_conversation.await_args.args[0]
    assert turn_arg.key_entities == []


@pytest.mark.asyncio
async def test_knowledge_entity_still_written_to_core(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    capture = _make_capture()
    extraction = _extraction([_knowledge_entity()])
    p1, p2, p3 = _patches(extraction)
    with (
        p1,
        p2,
        p3,
        patch(
            "personal_agent.second_brain.consolidator.get_default_sysgraph_repo",
            return_value=None,
        ),
    ):
        result = await consolidator._process_capture(capture)

    memory_service.create_entity.assert_awaited_once()
    assert result["entities_created"] == 1
    assert result["entities_dispatched_ephemeral"] == 0
    assert result["entities_dispatched_finding"] == 0
    turn_arg = memory_service.create_conversation.await_args.args[0]
    assert turn_arg.key_entities == ["Rafale"]


@pytest.mark.asyncio
async def test_mixed_turn_splits_per_item(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """One capture with one entity of each kind routes each to exactly one home."""
    capture = _make_capture()
    extraction = _extraction([_knowledge_entity(), _ephemeral_entity(), _finding_entity()])
    mock_repo = MagicMock()
    mock_repo.record_finding = AsyncMock(return_value=None)
    p1, p2, p3 = _patches(extraction)
    with (
        p1,
        p2,
        p3,
        patch(
            "personal_agent.second_brain.consolidator.get_default_sysgraph_repo",
            return_value=mock_repo,
        ),
    ):
        result = await consolidator._process_capture(capture)

    memory_service.create_entity.assert_awaited_once()
    mock_repo.record_finding.assert_awaited_once()
    assert result["entities_created"] == 1
    assert result["entities_dispatched_ephemeral"] == 1
    assert result["entities_dispatched_finding"] == 1
    turn_arg = memory_service.create_conversation.await_args.args[0]
    # Only the knowledge entity's name reaches key_entities -> no bare-node MERGE
    # for the ephemeral/finding names.
    assert turn_arg.key_entities == ["Rafale"]


@pytest.mark.asyncio
async def test_missing_output_kind_fails_open_to_knowledge(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    capture = _make_capture()
    entity = _knowledge_entity()
    del entity["output_kind"]
    extraction = _extraction([entity])
    p1, p2, p3 = _patches(extraction)
    with (
        p1,
        p2,
        p3,
        patch(
            "personal_agent.second_brain.consolidator.get_default_sysgraph_repo",
            return_value=None,
        ),
    ):
        result = await consolidator._process_capture(capture)

    memory_service.create_entity.assert_awaited_once()
    assert result["entities_created"] == 1


@pytest.mark.asyncio
async def test_finding_sysgraph_unavailable_degrades_gracefully(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    capture = _make_capture()
    extraction = _extraction([_finding_entity()])
    p1, p2, p3 = _patches(extraction)
    with (
        p1,
        p2,
        p3,
        patch(
            "personal_agent.second_brain.consolidator.get_default_sysgraph_repo",
            return_value=None,
        ),
    ):
        result = await consolidator._process_capture(capture)

    memory_service.create_entity.assert_not_awaited()
    assert result["entities_dispatched_finding"] == 0
    assert result["entities_dispatch_finding_failed"] == 1


@pytest.mark.asyncio
async def test_relationship_touching_dispatched_away_entity_is_skipped(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """A relationship whose endpoint was dispatched away must not reach Core either.

    Regression: create_relationship's Cypher MATCHes both endpoints by name — if the
    entity-creation loop above no longer writes a node for a dispatched-away
    (ephemeral/finding) name, an unfiltered relationships loop would either silently
    no-op (endpoint absent) or, worse, splice an edge onto an unrelated pre-existing
    Core entity that happens to share that name from a prior turn.
    """
    capture = _make_capture()
    extraction = _extraction(
        [_knowledge_entity(), _finding_entity()],
        relationships=[{"source": "Rafale", "target": "Postgres", "type": "RELATED_TO"}],
    )
    mock_repo = MagicMock()
    mock_repo.record_finding = AsyncMock(return_value=None)
    p1, p2, p3 = _patches(extraction)
    with (
        p1,
        p2,
        p3,
        patch(
            "personal_agent.second_brain.consolidator.get_default_sysgraph_repo",
            return_value=mock_repo,
        ),
    ):
        result = await consolidator._process_capture(capture)

    memory_service.create_relationship.assert_not_awaited()
    assert result["relationships_created"] == 0
    assert result["relationships_dispatch_skipped"] == 1


@pytest.mark.asyncio
async def test_relationship_between_two_knowledge_entities_still_created(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """Regression guard: the dispatch-skip check must not over-filter ordinary relationships."""
    capture = _make_capture()
    extraction = _extraction(
        [_knowledge_entity(name="Rafale"), _knowledge_entity(name="Dassault")],
        relationships=[{"source": "Rafale", "target": "Dassault", "type": "MANUFACTURED_BY"}],
    )
    p1, p2, p3 = _patches(extraction)
    with (
        p1,
        p2,
        p3,
        patch(
            "personal_agent.second_brain.consolidator.get_default_sysgraph_repo",
            return_value=None,
        ),
    ):
        result = await consolidator._process_capture(capture)

    memory_service.create_relationship.assert_awaited_once()
    assert result["relationships_created"] == 1
    assert result["relationships_dispatch_skipped"] == 0


@pytest.mark.asyncio
async def test_finding_sysgraph_write_failure_is_counted_not_raised(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    capture = _make_capture()
    extraction = _extraction([_finding_entity()])
    mock_repo = MagicMock()
    mock_repo.record_finding = AsyncMock(side_effect=RuntimeError("connection reset"))
    p1, p2, p3 = _patches(extraction)
    with (
        p1,
        p2,
        p3,
        patch(
            "personal_agent.second_brain.consolidator.get_default_sysgraph_repo",
            return_value=mock_repo,
        ),
    ):
        result = await consolidator._process_capture(capture)

    memory_service.create_entity.assert_not_awaited()
    assert result["entities_dispatched_finding"] == 0
    assert result["entities_dispatch_finding_failed"] == 1
