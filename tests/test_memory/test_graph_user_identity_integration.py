"""Integration proof for FRE-998 — identity is readable back out of a real graph.

This is the ticket's stated proof requirement at component altitude: *"verified by
querying the graph rather than by asserting the code path runs"*. It drives the
real :meth:`SecondBrainConsolidator.consolidate_recent_captures` against the real
Neo4j test substrate (:7688 via ``tests/conftest.py``), then reads the properties
back with Cypher.

Only the boundaries that are neither the graph nor the composition under test are
mocked: capture loading, the extraction LLM, the Postgres attempt ledger and the
promotion pipeline. Skips when the test Neo4j is unavailable, matching
``test_graph_structure.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.memory.service import MemoryService
from personal_agent.second_brain.consolidator import SecondBrainConsolidator

_MARKER = "fre998"


@pytest_asyncio.fixture
async def graph() -> Any:
    """Connected MemoryService against the test substrate; removes its own data."""
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    if not await service.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")
    yield service
    if service.driver:
        async with service.driver.session() as session:
            await session.run("MATCH (n) WHERE n.fre998_marker = $m DETACH DELETE n", m=_MARKER)
    await service.disconnect()


def _capture(session_id: str, user_id: uuid.UUID) -> TaskCapture:
    return TaskCapture(
        trace_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        user_message=f"{_MARKER} what is the capital of France?",
        assistant_response="Paris.",
        session_id=session_id,
        tools_used=[],
        duration_ms=12,
        outcome="completed",
        user_id=user_id,
    )


def _extraction(_capped: bool = False) -> dict[str, Any]:
    """A successful extraction result with no entities (keeps the graph small)."""
    return {
        "entities": [],
        "relationships": [],
        "entity_names": [],
        "summary": f"{_MARKER} a question about France",
    }


async def _run(
    consolidator: SecondBrainConsolidator, captures: list[TaskCapture]
) -> dict[str, Any]:
    """Drive the real consolidation entrypoint over ``captures``."""
    mod = "personal_agent.second_brain.consolidator"
    with (
        patch(f"{mod}.read_captures", return_value=captures),
        patch(
            f"{mod}.extract_entities_and_relationships",
            AsyncMock(side_effect=lambda *a, **k: _extraction()),
        ),
        patch(f"{mod}.previous_attempt_count", AsyncMock(return_value=0)),
        patch(f"{mod}.record_consolidation_attempt", AsyncMock(return_value=None)),
        patch(f"{mod}.run_promotion_pipeline", AsyncMock(return_value=MagicMock(promoted_count=0))),
    ):
        result: dict[str, Any] = await consolidator.consolidate_recent_captures()
    return result


async def _mark(graph: MemoryService, session_id: str | None, turn_ids: list[str]) -> None:
    """Tag written nodes so the fixture can delete exactly this test's data."""
    async with graph.driver.session() as db:
        await db.run(
            "MATCH (t:Turn) WHERE t.turn_id IN $ids SET t.fre998_marker = $m",
            ids=turn_ids,
            m=_MARKER,
        )
        if session_id:
            await db.run(
                "MATCH (s:Session {session_id: $sid}) SET s.fre998_marker = $m",
                sid=session_id,
                m=_MARKER,
            )


@pytest.mark.asyncio
async def test_new_session_and_turn_carry_identity_in_the_graph(graph: MemoryService) -> None:
    """AC-1: after a real consolidation, both nodes answer 'whose is this?'."""
    session_id = str(uuid.uuid4())
    user_id = uuid.uuid4()
    capture = _capture(session_id, user_id)
    consolidator = SecondBrainConsolidator(memory_service=graph)

    await _run(consolidator, [capture])
    await _mark(graph, session_id, [capture.trace_id])

    async with graph.driver.session() as db:
        row = await (
            await db.run(
                """
                MATCH (s:Session {session_id: $sid})
                OPTIONAL MATCH (t:Turn {turn_id: $tid})
                RETURN s.user_id AS session_user_id, t.user_id AS turn_user_id
                """,
                sid=session_id,
                tid=capture.trace_id,
            )
        ).single()

    assert row is not None, "no Session node was created for the consolidated session"
    assert row["session_user_id"] == str(user_id)
    assert row["turn_user_id"] == str(user_id)


@pytest.mark.asyncio
async def test_the_graph_alone_answers_whose_session_it_is(graph: MemoryService) -> None:
    """AC-3: the ticket's query shape, run against the graph with no other substrate."""
    session_id = str(uuid.uuid4())
    user_id = uuid.uuid4()
    capture = _capture(session_id, user_id)
    consolidator = SecondBrainConsolidator(memory_service=graph)

    await _run(consolidator, [capture])
    await _mark(graph, session_id, [capture.trace_id])

    async with graph.driver.session() as db:
        row = await (
            await db.run(
                "MATCH (s:Session {session_id: $sid}) RETURN s.user_id AS owner",
                sid=session_id,
            )
        ).single()

    assert row["owner"] == str(user_id)


@pytest.mark.asyncio
async def test_reconsolidation_does_not_erase_identity(graph: MemoryService) -> None:
    """The COALESCE guard, proven on a real node rather than by reading Cypher.

    Master's 2026-07-26 backfill attributed 118 existing sessions. A bare
    assignment would wipe that the next time one of them received a turn.
    """
    session_id = str(uuid.uuid4())
    user_id = uuid.uuid4()
    first = _capture(session_id, user_id)
    consolidator = SecondBrainConsolidator(memory_service=graph)
    await _run(consolidator, [first])

    # A later pass that cannot determine the owner (here: two captures disagreeing,
    # which fails closed and passes user_id=None) must leave the stored value alone.
    second = _capture(session_id, user_id)
    third = _capture(session_id, uuid.uuid4())
    await _run(consolidator, [second, third])
    await _mark(graph, session_id, [first.trace_id, second.trace_id, third.trace_id])

    async with graph.driver.session() as db:
        row = await (
            await db.run(
                "MATCH (s:Session {session_id: $sid}) RETURN s.user_id AS owner",
                sid=session_id,
            )
        ).single()

    assert row["owner"] == str(user_id)


@pytest.mark.asyncio
async def test_an_orphan_turn_still_carries_identity(graph: MemoryService) -> None:
    """The orphan case, in the exact shape that produced 1828 of them.

    ``scripts/replay_sessions_to_neo4j.py`` called ``_process_capture`` directly,
    so no Session node was ever created and the turn could never be linked. Under
    the old edge-only model such a turn was unattributable forever. Its identity
    must now survive on the turn itself, with no Session node in play.
    """
    session_id = str(uuid.uuid4())
    user_id = uuid.uuid4()
    capture = _capture(session_id, user_id)
    consolidator = SecondBrainConsolidator(memory_service=graph)

    mod = "personal_agent.second_brain.consolidator"
    with (
        patch(
            f"{mod}.extract_entities_and_relationships",
            AsyncMock(side_effect=lambda *a, **k: _extraction()),
        ),
        patch(f"{mod}.previous_attempt_count", AsyncMock(return_value=0)),
        patch(f"{mod}.record_consolidation_attempt", AsyncMock(return_value=None)),
    ):
        await consolidator._process_capture(capture)
    await _mark(graph, session_id, [capture.trace_id])

    async with graph.driver.session() as db:
        row = await (
            await db.run(
                """
                MATCH (t:Turn {turn_id: $tid})
                OPTIONAL MATCH (s:Session {session_id: $sid})
                RETURN t.user_id AS owner, s.session_id AS session_node
                """,
                tid=capture.trace_id,
                sid=session_id,
            )
        ).single()

    assert row is not None
    assert row["session_node"] is None, "no Session node should exist in the orphan shape"
    assert row["owner"] == str(user_id)
