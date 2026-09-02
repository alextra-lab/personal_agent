"""_read_before_emit_decision tests (ADR-0105 D9/FRE-721, FRE-1354).

Covers the shared helper used by the direct event->CONFIG_PROPOSAL handlers
(error-pattern, compaction-quality, graph-quality, staleness) in
``pipeline_handlers.py`` — these build a ``CaptainLogEntry`` straight from a
typed event rather than through ``InsightsEngine``, so they need their own
read-before-emit wiring.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.captains_log.corroboration import suppresses_proposal
from personal_agent.events.pipeline_handlers import (
    _read_before_emit_decision,
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
        result = await _read_before_emit_decision(
            source="statistical_detector",
            category="reliability",
            scope="tools",
            fingerprint="fp-suppress-test",
            what="w",
            why="y",
            how="h",
            trace_id=None,
        )
    # A connect failure leaves repo=None, so check_before_emit short-circuits to
    # GENERATE_NEW rather than the DEGRADED_* branch — either way it must fail open.
    assert suppresses_proposal(result, min_seen_count=3) is False


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
        result = await _read_before_emit_decision(
            source="statistical_detector",
            category="reliability",
            scope="tools",
            fingerprint="fp-suppress-test",
            what="w",
            why="y",
            how="h",
            trace_id=None,
        )
    assert suppresses_proposal(result, min_seen_count=3) is True
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
        result = await _read_before_emit_decision(
            source="statistical_detector",
            category="reliability",
            scope="tools",
            fingerprint="fp-suppress-test",
            what="w",
            why="y",
            how="h",
            trace_id=None,
        )
    assert suppresses_proposal(result, min_seen_count=3) is False


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
        "personal_agent.events.pipeline_handlers._read_before_emit_suppresses_entry",
        new=AsyncMock(return_value=True),
    ):
        handler = build_error_pattern_captain_log_handler(manager=manager)
        await handler(event)

    manager.save_entry.assert_not_called()


@pytest.mark.asyncio
async def test_detector_entry_keeps_and_stamps_a_corroborated_reinforcement() -> None:
    """FRE-1354: a detector's reinforced-above-bar proposal survives and is stamped.

    The four direct-event detectors erased their proposal on every reinforcement,
    exactly like reflection did, so a recurring anomaly could never promote however
    many times it was observed.
    """
    from datetime import datetime, timezone

    from personal_agent.captains_log.models import (
        CaptainLogEntry,
        CaptainLogEntryType,
        ChangeCategory,
        ChangeScope,
        ProposalSource,
        ProposedChange,
    )
    from personal_agent.events.pipeline_handlers import _read_before_emit_suppresses_entry

    canonical_first_seen = datetime(2026, 7, 7, tzinfo=timezone.utc)
    entry = CaptainLogEntry(
        entry_id="CL-detector",
        type=CaptainLogEntryType.CONFIG_PROPOSAL,
        title="Recurring anomaly",
        rationale="seen repeatedly",
        proposed_change=ProposedChange(
            what="Investigate the anomaly",
            why="it recurs",
            how="measure for 7 days",
            category=ChangeCategory.RELIABILITY,
            scope=ChangeScope.SECOND_BRAIN,
            source=ProposalSource.STATISTICAL_DETECTOR,
            fingerprint="this-sightings-hash",
            seen_count=1,
            first_seen=datetime.now(timezone.utc),
        ),
    )

    with patch(
        "personal_agent.events.pipeline_handlers._read_before_emit_decision",
        new=AsyncMock(
            return_value=ReadBeforeEmitResult(
                decision=ReadBeforeEmitDecision.REINFORCED,
                proposal_id="row-1",
                seen_count=33,
                fingerprint="canonical-identity",
                first_seen=canonical_first_seen,
            )
        ),
    ):
        suppressed = await _read_before_emit_suppresses_entry(
            entry,
            source="statistical_detector",
            category="reliability",
            scope="second_brain",
            fingerprint="this-sightings-hash",
            trace_id=None,
        )

    assert suppressed is False
    assert entry.proposed_change is not None
    assert entry.proposed_change.seen_count == 33
    assert entry.proposed_change.fingerprint == "canonical-identity"
    assert entry.proposed_change.first_seen == canonical_first_seen


@pytest.mark.asyncio
async def test_detector_entry_below_the_bar_is_still_suppressed() -> None:
    """FRE-1354: two sightings is not corroboration — unchanged suppression."""
    from personal_agent.events.pipeline_handlers import _read_before_emit_suppresses_entry

    entry = MagicMock()
    entry.proposed_change = None

    with patch(
        "personal_agent.events.pipeline_handlers._read_before_emit_decision",
        new=AsyncMock(
            return_value=ReadBeforeEmitResult(
                decision=ReadBeforeEmitDecision.REINFORCED, seen_count=2
            )
        ),
    ):
        suppressed = await _read_before_emit_suppresses_entry(
            entry,
            source="statistical_detector",
            category="reliability",
            scope="second_brain",
            fingerprint="fp",
            trace_id=None,
        )
    assert suppressed is True
