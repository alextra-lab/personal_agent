"""FRE-1034: get_trace_events must run off the event loop during reflection.

Root cause: generate_reflection_entry called the synchronous, potentially slow
get_trace_events directly, on the same asyncio event loop the reflection background
task itself runs on — stalling all concurrent requests for the duration of the file
read. This test uses the REAL asyncio.to_thread (not a mock that just forwards the
call inline) with a deliberately blocking get_trace_events stub, and proves the event
loop keeps making progress on other work while it runs.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.captains_log import reflection
from personal_agent.captains_log.models import CaptainLogEntry, CaptainLogEntryType

BLOCKING_CALL_SECONDS = 0.2
HEARTBEAT_INTERVAL_SECONDS = 0.01
HEARTBEAT_TICKS = 30


def _blocking_get_trace_events(trace_id: str) -> list[dict[str, Any]]:
    """Stand-in for the real get_trace_events: simulates the slow file-parse cost."""
    time.sleep(BLOCKING_CALL_SECONDS)
    return []


def _fast_dspy(*args: Any, **kwargs: Any) -> tuple[CaptainLogEntry, list[str]]:
    """Stand-in for generate_reflection_dspy: fast, no LLM call, no missing skills."""
    entry = CaptainLogEntry(
        entry_id="",
        type=CaptainLogEntryType.REFLECTION,
        title="t",
        rationale="r",
    )
    return entry, []


class TestReflectionTraceEventsThreadOffload:
    """generate_reflection_entry must not block the event loop while it reads traces."""

    @pytest.mark.asyncio
    async def test_blocking_get_trace_events_does_not_stall_the_event_loop(self) -> None:
        """A 200ms blocking get_trace_events call must not stall a concurrent heartbeat."""
        heartbeat_ticks: list[float] = []

        async def heartbeat() -> None:
            for _ in range(HEARTBEAT_TICKS):
                heartbeat_ticks.append(time.monotonic())
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

        with (
            patch.object(reflection, "get_trace_events", _blocking_get_trace_events),
            patch.object(reflection, "DSPY_AVAILABLE", True),
            patch.object(reflection, "generate_reflection_dspy", _fast_dspy),
            patch.object(reflection, "load_mean_rating_lookup", AsyncMock(return_value={})),
        ):
            await asyncio.gather(
                heartbeat(),
                reflection.generate_reflection_entry(
                    user_message="hi",
                    trace_id="trace-offload-test",
                    steps_count=1,
                    final_state="COMPLETED",
                    reply_length=5,
                ),
            )

        # The heartbeat ticks every ~10ms. If the 200ms blocking call ran on the event
        # loop (not offloaded to a thread), we'd see one gap of roughly 200ms between
        # consecutive ticks. Assert no such stall — the loop kept making progress.
        gaps = [b - a for a, b in zip(heartbeat_ticks, heartbeat_ticks[1:], strict=False)]
        assert max(gaps) < BLOCKING_CALL_SECONDS / 2, (
            f"event loop stalled for {max(gaps):.3f}s — get_trace_events blocked it "
            f"instead of running in a thread"
        )
