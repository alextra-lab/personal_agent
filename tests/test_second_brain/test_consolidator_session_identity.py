"""Unit tests for FRE-998 — the consolidator threads session identity to the graph.

``_consolidate_sessions`` has each capture's ``user_id`` in scope but never passed
it to ``create_session``, so Session nodes carried no identity and the graph could
not answer whose session a session was. These tests pin the pass-through and the
fail-closed behaviour when captures for one session disagree.

``MemoryService`` is mocked — the real-graph round trip lives in
``tests/test_memory/test_graph_user_identity_integration.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.second_brain.consolidator import SecondBrainConsolidator


def _capture(session_id: str, *, user_id: uuid.UUID, offset_s: int = 0) -> TaskCapture:
    """Build a minimal capture belonging to ``session_id`` and ``user_id``."""
    return TaskCapture(
        trace_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc) + timedelta(seconds=offset_s),
        user_message="hello",
        assistant_response="hi",
        session_id=session_id,
        tools_used=[],
        duration_ms=10,
        outcome="completed",
        user_id=user_id,
    )


@pytest.fixture
def consolidator() -> SecondBrainConsolidator:
    """A consolidator whose MemoryService is fully mocked."""
    svc = MagicMock()
    svc.create_session = AsyncMock(return_value=True)
    svc.link_session_turns = AsyncMock(return_value=1)
    svc.connected = False  # short-circuits _update_session_dominant_entities
    svc.driver = None
    return SecondBrainConsolidator(memory_service=svc)


class TestSessionIdentityPassThrough:
    @pytest.mark.asyncio
    async def test_capture_user_id_reaches_create_session(
        self, consolidator: SecondBrainConsolidator
    ) -> None:
        user_id = uuid.uuid4()
        captures = [_capture("s-1", user_id=user_id), _capture("s-1", user_id=user_id, offset_s=5)]

        created = await consolidator._consolidate_sessions(captures, {"s-1"}, trace_id="trace-abc")

        assert created == 1
        consolidator.memory_service.create_session.assert_awaited_once()
        assert consolidator.memory_service.create_session.await_args.kwargs["user_id"] == user_id

    @pytest.mark.asyncio
    async def test_each_session_gets_its_own_user(
        self, consolidator: SecondBrainConsolidator
    ) -> None:
        """One consolidation pass spans many users; identity must not bleed across sessions."""
        user_a, user_b = uuid.uuid4(), uuid.uuid4()
        captures = [_capture("s-a", user_id=user_a), _capture("s-b", user_id=user_b)]

        await consolidator._consolidate_sessions(captures, {"s-a", "s-b"}, trace_id="trace-abc")

        seen = {
            call.args[0].session_id: call.kwargs["user_id"]
            for call in consolidator.memory_service.create_session.await_args_list
        }
        assert seen == {"s-a": user_a, "s-b": user_b}


class TestMixedIdentityFailsClosed:
    @pytest.mark.asyncio
    async def test_disagreeing_captures_write_no_identity(
        self, consolidator: SecondBrainConsolidator
    ) -> None:
        """Two users on one session is an invariant violation, not an ambiguity.

        Picking a winner would be arbitrary, and because a non-null value always
        wins the COALESCE, a wrong pick silently overwrites correct identity.
        """
        captures = [
            _capture("s-1", user_id=uuid.uuid4()),
            _capture("s-1", user_id=uuid.uuid4(), offset_s=5),
        ]

        with patch("personal_agent.second_brain.consolidator.log") as mock_log:
            await consolidator._consolidate_sessions(captures, {"s-1"}, trace_id="trace-abc")

        assert consolidator.memory_service.create_session.await_args.kwargs["user_id"] is None
        errors = {call.args[0] for call in mock_log.error.call_args_list}
        assert "session_captures_mixed_user_id" in errors

    @pytest.mark.asyncio
    async def test_session_is_still_written(self, consolidator: SecondBrainConsolidator) -> None:
        """Failing closed on identity must not drop the Session node itself."""
        captures = [
            _capture("s-1", user_id=uuid.uuid4()),
            _capture("s-1", user_id=uuid.uuid4(), offset_s=5),
        ]

        created = await consolidator._consolidate_sessions(captures, {"s-1"}, trace_id="trace-abc")

        assert created == 1
