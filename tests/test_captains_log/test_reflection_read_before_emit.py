"""_apply_read_before_emit tests (ADR-0105 D9/FRE-721, reflection producer)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    CaptainLogStatus,
    ChangeCategory,
    ChangeScope,
    ProposalSource,
    ProposedChange,
)
from personal_agent.captains_log.reflection import _apply_read_before_emit
from personal_agent.sysgraph.dedup import ReadBeforeEmitDecision, ReadBeforeEmitResult


def _entry_with_proposal() -> CaptainLogEntry:
    return CaptainLogEntry(
        entry_id="CL-test",
        timestamp=datetime.now(timezone.utc),
        type=CaptainLogEntryType.REFLECTION,
        title="Task: test",
        rationale="because",
        proposed_change=ProposedChange(
            what="Add retry logic",
            why="Improves reliability",
            how="Wrap calls in tenacity",
            category=ChangeCategory.RELIABILITY,
            scope=ChangeScope.LLM_CLIENT,
            source=ProposalSource.REFLECTION,
            fingerprint="fp-reflection-test",
            first_seen=datetime.now(timezone.utc),
        ),
        status=CaptainLogStatus.AWAITING_APPROVAL,
        telemetry_refs=[],
    )


@pytest.mark.asyncio
async def test_no_proposed_change_is_a_noop() -> None:
    """An entry with no proposal never calls sysgraph at all."""
    entry = _entry_with_proposal()
    entry.proposed_change = None
    with patch(
        "personal_agent.captains_log.reflection.check_before_emit", new=AsyncMock()
    ) as mocked:
        await _apply_read_before_emit(entry, None, trace_id="t1")
    mocked.assert_not_called()


@pytest.mark.asyncio
async def test_decided_skip_nulls_out_proposed_change() -> None:
    """An equivalent already-decided kind suppresses the proposal (annotation-only per AC-9)."""
    entry = _entry_with_proposal()
    with patch(
        "personal_agent.captains_log.reflection.check_before_emit",
        new=AsyncMock(
            return_value=ReadBeforeEmitResult(decision=ReadBeforeEmitDecision.DECIDED_SKIP)
        ),
    ):
        await _apply_read_before_emit(entry, object(), trace_id="t1")  # type: ignore[arg-type]
    assert entry.proposed_change is None
    assert entry.rationale == "because"  # rationale/metrics still flow through


@pytest.mark.asyncio
async def test_reinforced_nulls_out_proposed_change() -> None:
    """An equivalent still-awaiting kind also suppresses the proposal."""
    entry = _entry_with_proposal()
    with patch(
        "personal_agent.captains_log.reflection.check_before_emit",
        new=AsyncMock(
            return_value=ReadBeforeEmitResult(decision=ReadBeforeEmitDecision.REINFORCED)
        ),
    ):
        await _apply_read_before_emit(entry, object(), trace_id="t1")  # type: ignore[arg-type]
    assert entry.proposed_change is None


@pytest.mark.asyncio
async def test_generate_new_and_degraded_leave_proposal_intact() -> None:
    """GENERATE_NEW and DEGRADED_GENERATE_NEW both proceed exactly as before this ticket."""
    for decision in (
        ReadBeforeEmitDecision.GENERATE_NEW,
        ReadBeforeEmitDecision.DEGRADED_GENERATE_NEW,
    ):
        entry = _entry_with_proposal()
        with patch(
            "personal_agent.captains_log.reflection.check_before_emit",
            new=AsyncMock(return_value=ReadBeforeEmitResult(decision=decision)),
        ):
            await _apply_read_before_emit(entry, object(), trace_id="t1")  # type: ignore[arg-type]
        assert entry.proposed_change is not None


@pytest.mark.asyncio
async def test_falls_back_to_shared_singleton_when_no_repo_passed() -> None:
    """sysgraph_repo=None resolves via get_default_sysgraph_repo(), not a hardcoded None."""
    entry = _entry_with_proposal()
    sentinel_repo = object()
    with (
        patch(
            "personal_agent.captains_log.reflection.get_default_sysgraph_repo",
            return_value=sentinel_repo,
        ) as mocked_getter,
        patch(
            "personal_agent.captains_log.reflection.check_before_emit",
            new=AsyncMock(
                return_value=ReadBeforeEmitResult(decision=ReadBeforeEmitDecision.GENERATE_NEW)
            ),
        ) as mocked_check,
    ):
        await _apply_read_before_emit(entry, None, trace_id="t1")
    mocked_getter.assert_called_once()
    assert mocked_check.call_args.args[0] is sentinel_repo
