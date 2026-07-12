"""FRE-864 / ADR-0115 D2: the consolidator threads entity_data["class"] into Entity.

FRE-863 made the extractor emit ``entity["class"] ∈ {World, Personal}`` per entity,
but the consolidator's ``Entity(...)`` construction dropped it — this is the gap
FRE-864 closes. These tests prove the wiring using entity dicts shaped exactly like
real extraction output (pattern: ``test_consolidator_claims_wiring.py``).
"""

# ruff: noqa: D103

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.memory.models import Entity
from personal_agent.second_brain.consolidator import SecondBrainConsolidator

_TURN_TS = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def _make_capture() -> TaskCapture:
    return TaskCapture(
        trace_id=str(uuid.uuid4()),
        timestamp=_TURN_TS,
        user_message="Dr. Chen is my cardiologist. GraphRAG combines KGs with RAG.",
        assistant_response="Noted.",
        session_id=str(uuid.uuid4()),
        tools_used=[],
        duration_ms=100,
        outcome="completed",
        user_id=uuid.uuid4(),
        eval_mode=False,
    )


def _extraction_with_mixed_classes() -> dict[str, Any]:
    """Mirror real FRE-863 extractor output: one Personal entity, one World entity."""
    return {
        "entities": [
            {
                "name": "Dr. Chen",
                "type": "Person",
                "class": "Personal",
                "output_kind": "knowledge",
                "description": "The user's cardiologist",
            },
            {
                "name": "GraphRAG",
                "type": "MethodOrConcept",
                "class": "World",
                "output_kind": "knowledge",
                "description": "Technique combining knowledge graphs with RAG",
            },
        ],
        "relationships": [],
        "entity_names": ["Dr. Chen", "GraphRAG"],
        "summary": "User mentioned their cardiologist and discussed GraphRAG.",
        "stances": [],
        "claims": [],
    }


@pytest.fixture
def memory_service() -> MagicMock:
    svc = MagicMock()
    svc.create_conversation = AsyncMock(return_value=None)
    svc.create_entity = AsyncMock(return_value="entity-id")
    svc.create_relationship = AsyncMock(return_value="rel-1")
    svc.fetch_turn_discusses_relationship_element_ids = AsyncMock(return_value=[])
    svc.assert_stance = AsyncMock(return_value=True)
    svc.assert_claim = AsyncMock(return_value="claim-id-1")
    return svc


@pytest.fixture
def consolidator(memory_service: MagicMock) -> SecondBrainConsolidator:
    return SecondBrainConsolidator(memory_service=memory_service)


@pytest.mark.asyncio
async def test_entity_class_threaded_from_extraction_to_entity_model(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """AC-1 seam: entity_data["class"] reaches the Entity object passed to create_entity."""
    capture = _make_capture()
    with (
        patch(
            "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
            new_callable=AsyncMock,
            return_value=_extraction_with_mixed_classes(),
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
    ):
        await consolidator._process_capture(capture)

    assert memory_service.create_entity.await_count == 2
    entities_by_name = {
        call.args[0].name: call.args[0] for call in memory_service.create_entity.await_args_list
    }
    assert isinstance(entities_by_name["Dr. Chen"], Entity)
    assert entities_by_name["Dr. Chen"].knowledge_class == "Personal"
    assert entities_by_name["GraphRAG"].knowledge_class == "World"
