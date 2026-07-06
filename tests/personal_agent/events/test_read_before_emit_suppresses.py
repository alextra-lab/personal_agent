"""_read_before_emit_suppresses tests (ADR-0105 D9/FRE-721).

Covers the shared helper used by the direct event->CONFIG_PROPOSAL handlers
(error-pattern, compaction-quality, graph-quality, staleness) in
``pipeline_handlers.py`` — these build a ``CaptainLogEntry`` straight from a
typed event rather than through ``InsightsEngine``, so they need their own
read-before-emit wiring.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.events.pipeline_handlers import (
    _read_before_emit_suppresses,
    build_error_pattern_captain_log_handler,
)
from personal_agent.sysgraph.dedup import ReadBeforeEmitDecision, ReadBeforeEmitResult


@pytest.mark.asyncio
async def test_sysgraph_connect_failure_never_suppresses() -> None:
    """A connect failure degrades open — never suppresses (unchanged behavior)."""
    with patch(
        "personal_agent.sysgraph.SysgraphRepository",
        side_effect=RuntimeError("connect failed"),
    ):
        suppressed = await _read_before_emit_suppresses(
            source="statistical_detector",
            category="reliability",
            scope="tools",
            fingerprint="fp-suppress-test",
            what="w",
            why="y",
            how="h",
            trace_id=None,
        )
    assert suppressed is False


@pytest.mark.asyncio
async def test_decided_skip_reports_suppressed() -> None:
    """An equivalent already-decided kind reports suppressed=True."""
    mock_repo = MagicMock()
    mock_repo.connect = AsyncMock()
    mock_repo.disconnect = AsyncMock()
    with (
        patch("personal_agent.sysgraph.SysgraphRepository", return_value=mock_repo),
        patch(
            "personal_agent.sysgraph.dedup.check_before_emit",
            new=AsyncMock(
                return_value=ReadBeforeEmitResult(decision=ReadBeforeEmitDecision.DECIDED_SKIP)
            ),
        ),
    ):
        suppressed = await _read_before_emit_suppresses(
            source="statistical_detector",
            category="reliability",
            scope="tools",
            fingerprint="fp-suppress-test",
            what="w",
            why="y",
            how="h",
            trace_id=None,
        )
    assert suppressed is True
    mock_repo.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_new_reports_not_suppressed() -> None:
    """Nothing equivalent exists -> reports suppressed=False (unchanged behavior)."""
    mock_repo = MagicMock()
    mock_repo.connect = AsyncMock()
    mock_repo.disconnect = AsyncMock()
    with (
        patch("personal_agent.sysgraph.SysgraphRepository", return_value=mock_repo),
        patch(
            "personal_agent.sysgraph.dedup.check_before_emit",
            new=AsyncMock(
                return_value=ReadBeforeEmitResult(decision=ReadBeforeEmitDecision.GENERATE_NEW)
            ),
        ),
    ):
        suppressed = await _read_before_emit_suppresses(
            source="statistical_detector",
            category="reliability",
            scope="tools",
            fingerprint="fp-suppress-test",
            what="w",
            why="y",
            how="h",
            trace_id=None,
        )
    assert suppressed is False


@pytest.mark.asyncio
async def test_error_pattern_handler_skips_save_entry_when_suppressed() -> None:
    """Wiring proof: the error-pattern handler actually skips save_entry when suppressed."""
    from datetime import datetime, timezone

    from personal_agent.events.models import ErrorPatternDetectedEvent

    manager = MagicMock()
    manager.save_entry = MagicMock(return_value=None)
    now = datetime.now(timezone.utc)
    event = ErrorPatternDetectedEvent(
        source_component="telemetry.error_monitor",
        trace_id=None,
        fingerprint="fp-error-pattern-suppress-test",
        component="tools.fetch_url",
        event_name="fetch_url_timeout",
        error_type="TimeoutError",
        level="ERROR",
        occurrences=12,
        first_seen=now,
        last_seen=now,
        window_hours=24,
        sample_trace_ids=["tid-1"],
        sample_messages=["Read timeout after 10s"],
    )

    with patch(
        "personal_agent.events.pipeline_handlers._read_before_emit_suppresses",
        new=AsyncMock(return_value=True),
    ):
        handler = build_error_pattern_captain_log_handler(manager=manager)
        await handler(event)

    manager.save_entry.assert_not_called()
