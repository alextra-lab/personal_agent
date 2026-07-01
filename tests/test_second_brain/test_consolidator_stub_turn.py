"""Unit tests for the FRE-380 stub-Turn cap in :class:`SecondBrainConsolidator`.

The consolidator historically skipped Neo4j writes entirely when entity
extraction returned a fallback result. FRE-380 (Stage 1) caps the retries
and writes a stub Turn after ``settings.consolidator_max_extraction_attempts``
so the capture becomes joinable in Neo4j even when semantic extraction never
succeeds.

These tests mock ``extract_entities_and_relationships`` and
``MemoryService.create_conversation`` so the logic is tested without a live
LLM or Neo4j stack. Integration coverage lives in
``test_consolidation_e2e.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.second_brain.consolidator import SecondBrainConsolidator


def _make_capture(trace_id: str | None = None, *, eval_mode: bool = False) -> TaskCapture:
    """Build a minimal TaskCapture for the cap-logic tests."""
    return TaskCapture(
        trace_id=trace_id or str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        user_message="What is the meaning of life?",
        assistant_response="42, of course.",
        session_id=str(uuid.uuid4()),
        tools_used=[],
        duration_ms=100,
        outcome="completed",
        user_id=uuid.uuid4(),
        eval_mode=eval_mode,
    )


def _fallback_extraction(capture: TaskCapture) -> dict[str, Any]:
    """Mirror the shape ``extract_entities_and_relationships`` returns on LLM crash."""
    return {
        "entities": [],
        "relationships": [],
        "entity_names": [],
        # Fallback summary == user_message[:200]; this is the sentinel the
        # consolidator uses to detect fallbacks.
        "summary": capture.user_message.strip()[:200],
    }


def _successful_extraction() -> dict[str, Any]:
    return {
        "entities": [{"name": "Life", "type": "Concept", "description": "abstract"}],
        "relationships": [],
        "entity_names": ["Life"],
        "summary": "Discussion about life",
    }


@pytest.fixture
def memory_service() -> MagicMock:
    """A MemoryService stand-in with the methods the consolidator calls."""
    svc = MagicMock()
    svc.create_conversation = AsyncMock(return_value=None)
    svc.create_entity = AsyncMock(return_value="entity-id-1")
    svc.create_relationship = AsyncMock(return_value="rel-elem-1")
    svc.fetch_turn_discusses_relationship_element_ids = AsyncMock(return_value=[])
    return svc


@pytest.fixture
def consolidator(memory_service: MagicMock) -> SecondBrainConsolidator:
    return SecondBrainConsolidator(memory_service=memory_service)


# ---------------------------------------------------------------------------
# Below-cap: extraction fallback → original skip-and-retry (no Turn written)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_below_cap_fallback_skips_neo4j_writes(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """Attempts under the cap continue the historical retry behavior."""
    capture = _make_capture()
    with (
        patch(
            "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
            new_callable=AsyncMock,
            return_value=_fallback_extraction(capture),
        ),
        patch(
            "personal_agent.second_brain.consolidator.previous_attempt_count",
            new_callable=AsyncMock,
            return_value=2,  # attempt_number will be 3 (below cap of 5)
        ),
        patch(
            "personal_agent.second_brain.consolidator.record_consolidation_attempt",
            new_callable=AsyncMock,
        ) as mock_record,
    ):
        result = await consolidator._process_capture(capture)

    assert result["turns_created"] == 0
    assert result["entities_created"] == 0
    memory_service.create_conversation.assert_not_awaited()
    mock_record.assert_awaited_once()
    assert mock_record.await_args.kwargs.get("outcome") == "extraction_returned_fallback"


# ---------------------------------------------------------------------------
# FRE-637: the turn timestamp is threaded into the extractor as turn_timestamp
# so a Claim/Stance observed_at is the turn time, not consolidation-run time.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_capture_passes_capture_timestamp_as_turn_timestamp(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """`_process_capture` threads `capture.timestamp` into the extractor (ADR-0098 D5)."""
    capture = _make_capture()
    with (
        patch(
            "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
            new_callable=AsyncMock,
            return_value=_successful_extraction(),
        ) as mock_extract,
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

    mock_extract.assert_awaited_once()
    assert mock_extract.await_args.kwargs.get("turn_timestamp") == capture.timestamp


# ---------------------------------------------------------------------------
# At-cap: extraction fallback → stub Turn written, outcome=extraction_capped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_cap_fallback_writes_stub_turn(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """At the retry cap, the consolidator writes a stub Turn instead of skipping."""
    capture = _make_capture()
    with (
        patch(
            "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
            new_callable=AsyncMock,
            return_value=_fallback_extraction(capture),
        ),
        patch(
            "personal_agent.second_brain.consolidator.previous_attempt_count",
            new_callable=AsyncMock,
            return_value=4,  # attempt_number = 5 = cap
        ),
        patch(
            "personal_agent.second_brain.consolidator.record_consolidation_attempt",
            new_callable=AsyncMock,
        ) as mock_record,
    ):
        result = await consolidator._process_capture(capture)

    assert result["turns_created"] == 1
    assert result["entities_created"] == 0
    assert result["entity_ids"] == []

    # Outcome recorded as extraction_capped.
    mock_record.assert_awaited_once()
    assert mock_record.await_args.kwargs.get("outcome") == "extraction_capped"

    # Stub Turn was written via create_conversation.
    memory_service.create_conversation.assert_awaited_once()
    turn_arg = memory_service.create_conversation.await_args.args[0]
    assert turn_arg.turn_id == capture.trace_id
    assert turn_arg.trace_id == capture.trace_id
    assert turn_arg.session_id == capture.session_id
    assert turn_arg.user_message == capture.user_message
    assert turn_arg.assistant_response == capture.assistant_response
    assert turn_arg.key_entities == []
    assert turn_arg.properties.get("extraction_outcome") == "capped_after_retries"
    assert turn_arg.properties.get("extraction_attempts") == 5

    # No entity-creation calls.
    memory_service.create_entity.assert_not_awaited()


# ---------------------------------------------------------------------------
# Above-cap: same behavior as at-cap (idempotent re-run does not double-write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_above_cap_still_writes_stub_idempotently(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """A capture re-processed past the cap still goes through the stub path.

    Real-world idempotency is enforced by ``conversation_exists()`` in
    ``MemoryService.create_conversation``; in this unit test we verify only
    that the consolidator takes the stub-write branch (not the retry branch)
    on every invocation.
    """
    capture = _make_capture()
    with (
        patch(
            "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
            new_callable=AsyncMock,
            return_value=_fallback_extraction(capture),
        ),
        patch(
            "personal_agent.second_brain.consolidator.previous_attempt_count",
            new_callable=AsyncMock,
            return_value=9,  # attempt_number = 10, well above cap
        ),
        patch(
            "personal_agent.second_brain.consolidator.record_consolidation_attempt",
            new_callable=AsyncMock,
        ) as mock_record,
    ):
        result = await consolidator._process_capture(capture)

    assert result["turns_created"] == 1
    assert mock_record.await_args.kwargs.get("outcome") == "extraction_capped"


# ---------------------------------------------------------------------------
# Successful extraction at or above cap: cap does not interfere with success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_extraction_unaffected_by_cap(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """If extraction succeeds even after many attempts, the success path runs."""
    capture = _make_capture()
    with (
        patch(
            "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
            new_callable=AsyncMock,
            return_value=_successful_extraction(),
        ),
        patch(
            "personal_agent.second_brain.consolidator.previous_attempt_count",
            new_callable=AsyncMock,
            return_value=7,  # well past cap, but extraction succeeded this time
        ),
        patch(
            "personal_agent.second_brain.consolidator.record_consolidation_attempt",
            new_callable=AsyncMock,
        ) as mock_record,
    ):
        result = await consolidator._process_capture(capture)

    # Real success path: 1 Turn + entities created, outcome != extraction_capped.
    assert result["turns_created"] == 1
    assert result["entities_created"] >= 1
    memory_service.create_conversation.assert_awaited_once()
    # The success path records `success`, never `extraction_capped`.
    outcomes = [c.kwargs.get("outcome") for c in mock_record.await_args_list]
    assert "extraction_capped" not in outcomes


# ---------------------------------------------------------------------------
# FRE-523: EVAL provenance stamped onto the Turn (success + stub paths)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_provenance_stamped_on_success_turn(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """A successfully-extracted eval capture stamps eval_mode on the Turn."""
    capture = _make_capture(eval_mode=True)
    with (
        patch(
            "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
            new_callable=AsyncMock,
            return_value=_successful_extraction(),
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

    turn_arg = memory_service.create_conversation.await_args.args[0]
    assert turn_arg.properties.get("eval_mode") is True


@pytest.mark.asyncio
async def test_eval_provenance_stamped_on_stub_turn(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """An at-cap eval capture stamps eval_mode on the stub Turn."""
    capture = _make_capture(eval_mode=True)
    with (
        patch(
            "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
            new_callable=AsyncMock,
            return_value=_fallback_extraction(capture),
        ),
        patch(
            "personal_agent.second_brain.consolidator.previous_attempt_count",
            new_callable=AsyncMock,
            return_value=4,  # attempt_number = 5 = cap
        ),
        patch(
            "personal_agent.second_brain.consolidator.record_consolidation_attempt",
            new_callable=AsyncMock,
        ),
    ):
        await consolidator._process_capture(capture)

    turn_arg = memory_service.create_conversation.await_args.args[0]
    assert turn_arg.properties.get("eval_mode") is True


@pytest.mark.asyncio
async def test_non_eval_turn_marks_provenance_false(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """A normal (non-eval) capture stamps eval_mode=False on the Turn."""
    capture = _make_capture(eval_mode=False)
    with (
        patch(
            "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
            new_callable=AsyncMock,
            return_value=_successful_extraction(),
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

    turn_arg = memory_service.create_conversation.await_args.args[0]
    assert turn_arg.properties.get("eval_mode") is False


# ---------------------------------------------------------------------------
# Custom cap via setting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_cap_setting_honored(
    consolidator: SecondBrainConsolidator, memory_service: MagicMock
) -> None:
    """The ``consolidator_max_extraction_attempts`` setting can lower the cap."""
    capture = _make_capture()

    mock_settings = MagicMock(consolidator_max_extraction_attempts=3)
    with (
        patch(
            "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
            new_callable=AsyncMock,
            return_value=_fallback_extraction(capture),
        ),
        patch(
            "personal_agent.second_brain.consolidator.previous_attempt_count",
            new_callable=AsyncMock,
            return_value=2,  # attempt_number = 3 = custom cap
        ),
        patch(
            "personal_agent.second_brain.consolidator.record_consolidation_attempt",
            new_callable=AsyncMock,
        ) as mock_record,
        patch(
            "personal_agent.second_brain.consolidator.get_settings",
            return_value=mock_settings,
        ),
    ):
        result = await consolidator._process_capture(capture)

    assert result["turns_created"] == 1
    assert mock_record.await_args.kwargs.get("outcome") == "extraction_capped"
    memory_service.create_conversation.assert_awaited_once()
