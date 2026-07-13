"""ADR-0107 D5/Risk regression: consolidation's per-capture identity binding.

``consolidate_recent_captures`` processes captures from many users in a single
background pass (triggered by the scheduler's lifecycle loop or the
``request.captured`` event consumer — never from within a live request's own
``structlog.contextvars`` scope). Each capture must have its OWN
trace_id/session_id/user_id bound for the duration it is processed, and that
binding must not bleed into the next capture's log lines — a stale bind would
be a *wrong* user_id, which ADR-0107 AC-3a treats as a hard failure, not a
tolerable gap.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.contextvars
import structlog.testing

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.second_brain.consolidator import SecondBrainConsolidator


def _make_capture(*, user_id: uuid.UUID, session_id: str, trace_id: str) -> TaskCapture:
    return TaskCapture(
        trace_id=trace_id,
        timestamp=datetime.now(timezone.utc),
        user_message="hello",
        assistant_response="hi",
        session_id=session_id,
        tools_used=[],
        duration_ms=100,
        outcome="completed",
        user_id=user_id,
    )


@pytest.fixture
def memory_service() -> MagicMock:
    """A MemoryService stand-in with just the methods the loop calls."""
    svc = MagicMock()
    svc.connected = True
    svc.turn_exists = AsyncMock(return_value=False)
    return svc


@pytest.mark.asyncio
async def test_each_capture_binds_its_own_identity_not_the_previous_ones(
    memory_service: MagicMock,
) -> None:
    """Two captures from different users in one run must not cross-contaminate."""
    capture_a = _make_capture(
        user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        session_id="session-a",
        trace_id="trace-a",
    )
    capture_b = _make_capture(
        user_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        session_id="session-b",
        trace_id="trace-b",
    )

    consolidator = SecondBrainConsolidator(memory_service=memory_service)

    with (
        patch(
            "personal_agent.second_brain.consolidator.read_captures",
            return_value=[capture_a, capture_b],
        ),
        patch.object(consolidator, "_process_capture", AsyncMock(return_value={})),
        structlog.testing.capture_logs(
            processors=[structlog.contextvars.merge_contextvars]
        ) as captured,
    ):
        await consolidator.consolidate_recent_captures(days=7)

    per_capture_logs = {
        entry["trace_id"]: entry
        for entry in captured
        if entry.get("event") == "consolidation_processing_capture"
    }

    assert per_capture_logs["trace-a"]["user_id"] == str(capture_a.user_id)
    assert per_capture_logs["trace-a"]["session_id"] == "session-a"
    assert per_capture_logs["trace-b"]["user_id"] == str(capture_b.user_id)
    assert per_capture_logs["trace-b"]["session_id"] == "session-b"
    # The zero-tolerance-for-a-wrong-value bar (AC-3a): capture B's log must
    # never carry capture A's user_id, and vice versa.
    assert per_capture_logs["trace-a"]["user_id"] != per_capture_logs["trace-b"]["user_id"]


@pytest.mark.asyncio
async def test_capture_processing_failure_does_not_leak_identity_to_next_capture(
    memory_service: MagicMock,
) -> None:
    """bound_contextvars must reset even when _process_capture raises."""
    capture_a = _make_capture(
        user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        session_id="session-a",
        trace_id="trace-a",
    )
    capture_b = _make_capture(
        user_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        session_id="session-b",
        trace_id="trace-b",
    )

    consolidator = SecondBrainConsolidator(memory_service=memory_service)
    failing_then_ok = AsyncMock(side_effect=[RuntimeError("boom"), {}])

    with (
        patch(
            "personal_agent.second_brain.consolidator.read_captures",
            return_value=[capture_a, capture_b],
        ),
        patch.object(consolidator, "_process_capture", failing_then_ok),
        structlog.testing.capture_logs(
            processors=[structlog.contextvars.merge_contextvars]
        ) as captured,
    ):
        await consolidator.consolidate_recent_captures(days=7)

    failure_log = next(e for e in captured if e.get("event") == "capture_processing_failed")
    # "consolidation_capture_done" only fires on the successful path — capture_b's,
    # since capture_a's _process_capture call raised before reaching it.
    success_log = next(e for e in captured if e.get("event") == "consolidation_capture_done")

    assert failure_log["user_id"] == str(capture_a.user_id)
    assert success_log["user_id"] == str(capture_b.user_id)
