"""FRE-638: the consolidator wires extractor stances[]/claims[] into Core.

FRE-637 made the extractor emit ``stances`` and ``claims`` (provenance-stamped) but
left them inert. These tests prove ``_process_capture`` now resolves each into a
``Stance``/``Claim`` and calls ``assert_stance``/``assert_claim`` — with the owner
sentinel and provenance timestamp threaded — using mocked MemoryService methods.
"""

# ruff: noqa: D103

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.memory.models import Claim, Stance
from personal_agent.second_brain.consolidator import SecondBrainConsolidator

_TURN_TS = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def _make_capture() -> TaskCapture:
    return TaskCapture(
        trace_id=str(uuid.uuid4()),
        timestamp=_TURN_TS,
        user_message="I love the RAV4 Hybrid; my lease ends in March.",
        assistant_response="Noted.",
        session_id=str(uuid.uuid4()),
        tools_used=[],
        duration_ms=100,
        outcome="completed",
        user_id=uuid.uuid4(),
        eval_mode=False,
    )


def _extraction_with_stance_and_claim(capture: TaskCapture) -> dict[str, Any]:
    """Mirror the FRE-637 extractor output: entities + one stance + one claim."""
    prov = {
        "trace_id": capture.trace_id,
        "session_id": capture.session_id,
        "source_type": "conversation",
        "observed_at": capture.timestamp.isoformat(),
        "extracted_at": capture.timestamp.isoformat(),
    }
    return {
        "entities": [
            {
                "name": "Toyota RAV4 Hybrid",
                "type": "Technology",
                "class": "World",
                "description": "car",
                # FRE-725: per-entity description signal threaded into create_entity.
                "description_update_kind": "enrichment",
            }
        ],
        "relationships": [],
        "entity_names": ["Toyota RAV4 Hybrid"],
        "summary": "User discussed the RAV4 and their lease.",
        "stances": [
            {
                "subject": "owner",
                "target": "Toyota RAV4 Hybrid",
                "affect": "loves it",
                "mastery": None,
                "description": "strong preference",
                "class": "Stance",
                "provenance": dict(prov),
            }
        ],
        "claims": [
            {
                "subject": "owner",
                "content": "The user's car lease ends in March.",
                "facet": "lease_end_date",
                "update_kind": "new",
                "description": "timing constraint",
                "class": "Personal",
                "provenance": dict(prov),
            }
        ],
    }


@pytest.fixture
def memory_service() -> MagicMock:
    svc = MagicMock()
    svc.create_conversation = AsyncMock(return_value=None)
    svc.create_entity = AsyncMock(return_value="Toyota RAV4 Hybrid")
    svc.create_relationship = AsyncMock(return_value="rel-1")
    svc.fetch_turn_discusses_relationship_element_ids = AsyncMock(return_value=[])
    svc.assert_stance = AsyncMock(return_value=True)
    svc.assert_claim = AsyncMock(return_value="claim-id-1")
    return svc


@pytest.fixture
def consolidator(memory_service: MagicMock) -> SecondBrainConsolidator:
    return SecondBrainConsolidator(memory_service=memory_service)


@pytest.mark.asyncio
async def test_stance_and_claim_are_wired_into_core(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    capture = _make_capture()
    with (
        patch(
            "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
            new_callable=AsyncMock,
            return_value=_extraction_with_stance_and_claim(capture),
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
        result = await consolidator._process_capture(capture)

    memory_service.assert_stance.assert_awaited_once()
    stance_arg = memory_service.assert_stance.await_args.args[0]
    assert isinstance(stance_arg, Stance)
    assert stance_arg.target == "Toyota RAV4 Hybrid"
    # observed_at is the turn time (FRE-637 provenance), the bitemporal axis.
    assert stance_arg.observed_at == _TURN_TS

    memory_service.assert_claim.assert_awaited_once()
    claim_arg = memory_service.assert_claim.await_args.args[0]
    assert isinstance(claim_arg, Claim)
    assert claim_arg.content == "The user's car lease ends in March."
    assert claim_arg.knowledge_class == "Personal"
    assert claim_arg.facet == "lease_end_date"  # FRE-712: facet threaded
    assert claim_arg.update_kind == "new"  # FRE-712: update_kind threaded
    assert claim_arg.observed_at == _TURN_TS
    # conversation source → 0.8 confidence (KnowledgeWeight).
    assert claim_arg.confidence == pytest.approx(0.8)

    assert result["stances_created"] == 1
    assert result["claims_created"] == 1

    # FRE-711: the World-description correction gate inputs are threaded into create_entity.
    memory_service.create_entity.assert_awaited_once()
    ce_kwargs = memory_service.create_entity.await_args.kwargs
    assert ce_kwargs["eval_mode"] is False  # capture.eval_mode
    assert ce_kwargs["description_confidence"] == pytest.approx(0.8)  # conversation source
    # FRE-725: the per-entity enrichment/correction signal is threaded from the entity dict.
    assert ce_kwargs["description_update_kind"] == "enrichment"


@pytest.mark.asyncio
async def test_no_stances_or_claims_is_a_noop(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    capture = _make_capture()
    extraction = _extraction_with_stance_and_claim(capture)
    extraction["stances"] = []
    extraction["claims"] = []
    with (
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
    ):
        result = await consolidator._process_capture(capture)

    memory_service.assert_stance.assert_not_awaited()
    memory_service.assert_claim.assert_not_awaited()
    assert result["stances_created"] == 0
    assert result["claims_created"] == 0
