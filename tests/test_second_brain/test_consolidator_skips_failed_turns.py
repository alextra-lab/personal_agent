"""FRE-1527: a failed turn's capture must not reach knowledge-graph consolidation.

FRE-1527 made every failed turn write a capture (``outcome="failed"``), for
analysability — captures were previously written only for ``COMPLETED`` turns.
Before this test, ``consolidate_recent_captures`` had no filter on
``capture.outcome`` at all, so a failed turn's (often empty, or partial +
generic-error-suffixed) ``assistant_response`` would be entity-extracted and
written into the graph as if it were a real answer. Caught in master's review
of PR #1183.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.second_brain.consolidator import SecondBrainConsolidator


def _make_capture(*, outcome: str, assistant_response: str | None) -> TaskCapture:
    return TaskCapture(
        trace_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        user_message="What model powers the SLM server?",
        assistant_response=assistant_response,
        outcome=outcome,
        user_id=uuid.uuid4(),
    )


class TestConsolidationSkipsFailedTurns:
    @pytest.mark.asyncio
    async def test_failed_capture_is_skipped_before_dedup_or_extraction(self) -> None:
        memory_service = MagicMock()
        memory_service.connected = True
        memory_service.turn_exists = AsyncMock(return_value=False)
        consolidator = SecondBrainConsolidator(memory_service=memory_service)

        failed_capture = _make_capture(
            outcome="failed",
            assistant_response=(
                "This turn ran out of context room in the model's window. "
                "Start a new turn, or ask a narrower question."
            ),
        )

        with patch(
            "personal_agent.second_brain.consolidator.read_captures",
            return_value=[failed_capture],
        ):
            result = await consolidator.consolidate_recent_captures(days=7)

        assert result["captures_processed"] == 1
        assert result["captures_skipped"] == 1
        assert result["turns_created"] == 0
        assert result["entities_created"] == 0
        # The skip fires before the dedup check — a failed capture never even
        # asks whether it was already consolidated.
        memory_service.turn_exists.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_capture_with_no_salvaged_reply_is_also_skipped(self) -> None:
        """A failed turn with nothing salvaged has ``assistant_response=None``

        (no tool_results to build a partial reply from) — the outcome check
        alone must be enough to skip it, with no dependence on response content.
        """
        memory_service = MagicMock()
        memory_service.connected = True
        memory_service.turn_exists = AsyncMock(return_value=False)
        consolidator = SecondBrainConsolidator(memory_service=memory_service)

        failed_capture = _make_capture(outcome="failed", assistant_response=None)

        with patch(
            "personal_agent.second_brain.consolidator.read_captures",
            return_value=[failed_capture],
        ):
            result = await consolidator.consolidate_recent_captures(days=7)

        assert result["captures_skipped"] == 1
        memory_service.turn_exists.assert_not_called()
