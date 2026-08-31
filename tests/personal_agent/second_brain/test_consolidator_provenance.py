"""FRE-1346 — the consolidator carries the address to the KG write (ADR-0098 A3/A4).

The defect this closes is propagation, not capture: ``tool_results`` already holds every
URL the turn fetched (FRE-947), and extraction never looked. These prove the consolidator
now derives sources from the capture, associates each extracted item by containment, and
hands the matched records to the write — with the two properties A3 insists on, that the
address travels **in the record** rather than by session ledger (AC-2) or by bus (AC-3).
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
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.types import ToolDefinition

_TS = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
_URL = "https://example.com/platforms"
_PAGE = (
    "SafeCart is a checkout platform used by retailers. "
    "The SafeCart engineering team is based in Lisbon."
)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="fetch_url",
            description="fetch",
            category="network",
            parameters=[],
            risk_level="medium",
            allowed_modes=["NORMAL"],
            referent_parameter="url",
        ),
        lambda **_: None,
    )
    return registry


def _capture(*, with_tool_results: bool = True) -> TaskCapture:
    return TaskCapture(
        trace_id="trace-1",
        session_id="session-A",
        timestamp=_TS,
        user_message="What is SafeCart?",
        assistant_response="SafeCart is a checkout platform based in Lisbon.",
        tools_used=["fetch_url"] if with_tool_results else [],
        duration_ms=100,
        outcome="completed",
        user_id=uuid.uuid4(),
        tool_results=(
            [
                {
                    "tool_name": "fetch_url",
                    "success": True,
                    "output": {"url": _URL, "text": _PAGE, "truncated": False},
                    "error": None,
                    "latency_ms": 12.0,
                    "arguments": {"url": _URL},
                }
            ]
            if with_tool_results
            else []
        ),
    )


def _extraction() -> dict[str, Any]:
    return {
        "entities": [
            {"name": "SafeCart", "type": "Organization", "class": "World", "description": "x"},
            {"name": "Kubernetes", "type": "Technology", "class": "World", "description": "y"},
        ],
        "relationships": [
            {"source": "SafeCart", "target": "Lisbon", "type": "BASED_IN"},
            {"source": "SafeCart", "target": "Berlin", "type": "BASED_IN"},
        ],
        "entity_names": ["SafeCart", "Kubernetes"],
        "summary": "A discussion of SafeCart.",
    }


def _consolidator() -> tuple[SecondBrainConsolidator, MagicMock]:
    memory_service = MagicMock()
    memory_service.connected = True
    memory_service.create_entity = AsyncMock(side_effect=lambda entity, **_: entity.name)
    memory_service.create_conversation = AsyncMock(return_value=True)
    memory_service.create_relationship = AsyncMock(return_value="4:abc:1")
    memory_service.assert_claim = AsyncMock(return_value="claim-1")
    memory_service.assert_stance = AsyncMock(return_value=True)
    memory_service.fetch_turn_discusses_relationship_element_ids = AsyncMock(return_value=[])
    consolidator = SecondBrainConsolidator(memory_service=memory_service, tool_registry=_registry())
    return consolidator, memory_service


async def _process(consolidator: SecondBrainConsolidator, capture: TaskCapture) -> dict[str, Any]:
    with (
        patch(
            "personal_agent.second_brain.consolidator.extract_entities_and_relationships",
            new=AsyncMock(return_value=_extraction()),
        ),
        patch(
            "personal_agent.second_brain.consolidator.previous_attempt_count",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "personal_agent.second_brain.consolidator.record_consolidation_attempt",
            new=AsyncMock(return_value=None),
        ),
    ):
        return await consolidator._process_capture(capture)


def _entity_sources(memory_service: MagicMock, name: str) -> list[Any]:
    for call in memory_service.create_entity.await_args_list:
        if call.args[0].name == name:
            return list(call.kwargs.get("source_records", []))
    raise AssertionError(f"create_entity was never called for {name}")


def _relationship_sources(memory_service: MagicMock, target: str) -> list[Any]:
    for call in memory_service.create_relationship.await_args_list:
        if call.args[0].target_id == target:
            return list(call.kwargs.get("source_records", []))
    raise AssertionError(f"create_relationship was never called for {target}")


# --------------------------------------------------------------------------------------
# AC-1 — the reference reaches the write, and it actually supports the item
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_contained_entity_is_written_with_the_fetched_source() -> None:
    consolidator, memory_service = _consolidator()
    await _process(consolidator, _capture())

    sources = _entity_sources(memory_service, "SafeCart")
    assert [s.referent for s in sources] == [_URL]


@pytest.mark.asyncio
async def test_seeded_negative_an_uncontained_entity_gets_no_source() -> None:
    """AC-1(b): the turn-level shortcut would attribute Kubernetes to the fetched page."""
    consolidator, memory_service = _consolidator()
    await _process(consolidator, _capture())

    assert _entity_sources(memory_service, "Kubernetes") == []


@pytest.mark.asyncio
async def test_a_contained_relationship_is_written_with_the_fetched_source() -> None:
    consolidator, memory_service = _consolidator()
    await _process(consolidator, _capture())

    assert [s.referent for s in _relationship_sources(memory_service, "Lisbon")] == [_URL]


@pytest.mark.asyncio
async def test_seeded_negative_an_uncontained_relationship_gets_no_source() -> None:
    """AC-1(b) for relationships — 'SafeCart based in Berlin' is in no fetched page."""
    consolidator, memory_service = _consolidator()
    await _process(consolidator, _capture())

    assert _relationship_sources(memory_service, "Berlin") == []


# --------------------------------------------------------------------------------------
# AC-2 / AC-3 — A3's two prohibitions
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac2_provenance_is_written_from_a_capture_read_after_session_end() -> None:
    """AC-2: the fetch->consolidation window is not bounded by a session.

    ``_process_capture`` takes only the persisted ``TaskCapture``; there is no live
    session object, request context, or in-memory ledger in scope. A session-scoped
    ledger design — the one A3 rejects — could not satisfy this, because the sweep runs
    ``consolidate_recent_captures(days=7)`` across sessions and process restarts.
    """
    consolidator, memory_service = _consolidator()
    capture = _capture()
    assert capture.session_id == "session-A"

    await _process(consolidator, capture)

    sources = _entity_sources(memory_service, "SafeCart")
    assert [s.referent for s in sources] == [_URL]


@pytest.mark.asyncio
async def test_ac3_provenance_is_written_with_the_event_bus_disabled() -> None:
    """AC-3: absent here would prove it travelled by bus, which A3 forbids."""
    consolidator, memory_service = _consolidator()

    with patch(
        "personal_agent.second_brain.consolidator.get_event_bus",
        new=MagicMock(side_effect=AssertionError("consolidation must not need the bus")),
    ):
        await _process(consolidator, _capture())

    assert [s.referent for s in _entity_sources(memory_service, "SafeCart")] == [_URL]


# --------------------------------------------------------------------------------------
# Reporting — the false-negative rate is countable, not silent (A4/A5)
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_none_rate_is_reported_against_the_population_that_had_a_source() -> None:
    consolidator, _ = _consolidator()
    result = await _process(consolidator, _capture())

    assert result["entities_provenanced"] == 1
    assert result["entities_none_with_sources"] == 1
    assert result["relationships_provenanced"] == 1
    assert result["relationships_none_with_sources"] == 1


@pytest.mark.asyncio
async def test_a_capture_with_no_referent_results_reports_no_none_population() -> None:
    """Items from a turn that fetched nothing are not false negatives — they had no
    source to be contained in, so counting them would inflate the rate.
    """
    consolidator, memory_service = _consolidator()
    result = await _process(consolidator, _capture(with_tool_results=False))

    assert result["entities_provenanced"] == 0
    assert result["entities_none_with_sources"] == 0
    assert _entity_sources(memory_service, "SafeCart") == []
