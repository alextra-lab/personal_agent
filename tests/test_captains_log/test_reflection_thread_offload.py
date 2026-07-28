"""FRE-1034: reflection's trace-event fetch — ES hot path, file fallback.

Master's bounce on PR #731 measured the fallback-only fix's actual concurrency
behavior against the live corpus: the threaded file scan does not parallelize
(1/2/4/8 concurrent -> 0.113s/0.352s/0.706s/1.544s, linear — the GIL serializes
the pure-Python `line_filter not in line` scan even off the event loop), while an
Elasticsearch term query does (0.037s/0.036s/0.124s/0.190s over the same range).
So the per-turn hot path (reflection.py) now queries Elasticsearch directly with a
plain `await` — genuine async I/O, not a thread offload — and only falls back to
the thread-offloaded file path (kept exactly as shipped in the fallback-only PR,
unchanged, still serving the CLI/eval callers) if Elasticsearch is unreachable.

agent-logs-* has index.refresh_interval=5s, and reflection fires ~1.5s after task
completion — inside that window. A live check on the real corpus (see the PR/ticket
comment) confirmed this exactly: 0/6 docs visible at 1.5s after write, 6/6 at 5.5s.
So the ES fetch waits out the window before querying (ES_REFRESH_WAIT_SECONDS);
this is free because reflection is fire-and-forget with nothing awaiting it.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
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
    """Stand-in for the real file-based get_trace_events: simulates the parse cost."""
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


class TestFetchTraceEventsEsHotPath:
    """_fetch_trace_events: ES first, file fallback only on ES failure."""

    @pytest.mark.asyncio
    async def test_uses_es_result_and_never_touches_the_file_path(self) -> None:
        """When ES succeeds, the file-based get_trace_events must not be called at all."""
        es_events = [
            {"event": "task_started", "trace_id": "t1", "timestamp": "2026-01-01T00:00:00"}
        ]
        mock_get_trace_events = AsyncMock(return_value=es_events)

        with (
            patch.object(reflection, "ES_REFRESH_WAIT_SECONDS", 0.0),
            patch(
                "personal_agent.telemetry.TelemetryQueries.get_trace_events",
                mock_get_trace_events,
            ),
            patch.object(reflection, "get_trace_events") as mock_file_get_trace_events,
        ):
            result = await reflection._fetch_trace_events("t1")

        assert result == es_events
        mock_get_trace_events.assert_awaited_once_with("t1")
        mock_file_get_trace_events.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_file_path_when_es_unreachable(self) -> None:
        """ES failure must be caught and logged, then fall back to the file path."""
        file_events = [{"event": "task_started", "trace_id": "t1", "timestamp": "x"}]

        with (
            patch.object(reflection, "ES_REFRESH_WAIT_SECONDS", 0.0),
            patch(
                "personal_agent.telemetry.TelemetryQueries.get_trace_events",
                AsyncMock(side_effect=RuntimeError("es unreachable")),
            ),
            patch.object(reflection, "get_trace_events", return_value=file_events),
        ):
            result = await reflection._fetch_trace_events("t1")

        assert result == file_events

    @pytest.mark.asyncio
    async def test_waits_out_the_refresh_window_before_querying(self) -> None:
        """The ES query must not fire until ES_REFRESH_WAIT_SECONDS has elapsed."""
        sleep_calls: list[float] = []
        real_sleep = asyncio.sleep

        async def spy_sleep(delay: float) -> None:
            sleep_calls.append(delay)
            await real_sleep(0)  # don't actually wait — just record the call

        with (
            patch("personal_agent.captains_log.reflection.asyncio.sleep", spy_sleep),
            patch(
                "personal_agent.telemetry.TelemetryQueries.get_trace_events",
                AsyncMock(return_value=[]),
            ),
        ):
            await reflection._fetch_trace_events("t1")

        assert sleep_calls == [reflection.ES_REFRESH_WAIT_SECONDS]


class TestEsRefreshWaitExceedsConfiguredInterval:
    """The wait constant must stay ahead of agent-logs-*'s actual refresh_interval."""

    def test_wait_exceeds_index_template_refresh_interval(self) -> None:
        """ES_REFRESH_WAIT_SECONDS must be read from, and exceed, the real template value."""
        template_path = (
            pathlib.Path(__file__).resolve().parents[2]
            / "docker"
            / "elasticsearch"
            / "index-template.json"
        )
        template = json.loads(template_path.read_text())
        refresh_interval_str = template["template"]["settings"]["index.refresh_interval"]
        assert refresh_interval_str.endswith("s")
        refresh_interval_seconds = float(refresh_interval_str[:-1])

        assert reflection.ES_REFRESH_WAIT_SECONDS > refresh_interval_seconds, (
            f"ES_REFRESH_WAIT_SECONDS ({reflection.ES_REFRESH_WAIT_SECONDS}s) must exceed "
            f"agent-logs-*'s configured refresh_interval ({refresh_interval_seconds}s), or a "
            f"freshly-written trace's newest events will still be invisible when queried"
        )


class TestReflectionTraceEventsThreadOffload:
    """Fallback path: generate_reflection_entry must not block the event loop."""

    @pytest.mark.asyncio
    async def test_blocking_file_fallback_does_not_stall_the_event_loop(self) -> None:
        """A 200ms blocking file-fallback read must not stall a concurrent heartbeat.

        Forces the ES attempt to fail so generate_reflection_entry exercises the
        thread-offloaded file-fallback path (kept unchanged from the fallback-only
        PR) — proving that fallback still doesn't block the event loop either.
        """
        heartbeat_ticks: list[float] = []

        async def heartbeat() -> None:
            for _ in range(HEARTBEAT_TICKS):
                heartbeat_ticks.append(time.monotonic())
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

        with (
            patch.object(reflection, "ES_REFRESH_WAIT_SECONDS", 0.0),
            patch(
                "personal_agent.telemetry.TelemetryQueries.get_trace_events",
                AsyncMock(side_effect=RuntimeError("es unreachable")),
            ),
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
