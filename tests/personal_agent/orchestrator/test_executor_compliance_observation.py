"""The compliance metric wired into the turn path (ADR-0138 D5, FRE-1284).

``_record_grounding`` is where an attempt's verdict is final and where
``ctx.grounding_record`` is about to be overwritten by the next D4 attempt, so it is the
only place attempt 1's observation can be taken. These tests drive that seam: which turns
become observations, which model they are credited to, and that a store failure stays
inside the background task instead of reaching the turn.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.captains_log.background import wait_for_background_tasks
from personal_agent.captains_log.turn_evidence import GroundingRecord
from personal_agent.governance.models import Mode
from personal_agent.grounding.verification import TurnEvidenceClass
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.executor import _record_compliance_observation
from personal_agent.orchestrator.types import ExecutionContext

MODEL = "gemma-3-27b-it-qat"


def _ctx(*, model_key: str | None = MODEL, trace_id: str = "trace-1284") -> ExecutionContext:
    """A context carrying only what the observation writer reads."""
    ctx = ExecutionContext(
        trace_id=trace_id,
        session_id="session-compliance",
        user_message="How many people live in Paris?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    ctx.answering_model_key = model_key
    return ctx


def _record(
    *,
    available: bool = True,
    non_exempt_count: int = 2,
    retrieval_forced: bool = False,
    compliant: bool = True,
) -> GroundingRecord:
    """A grounding record with the fields the eligibility predicate reads."""
    return GroundingRecord(
        mode="observe",
        available=available,
        non_exempt_count=non_exempt_count,
        passed_count=non_exempt_count if compliant else 0,
        retrieval_forced=retrieval_forced,
        first_generation_compliant=compliant,
        attempts=1,
    )


@pytest.mark.asyncio
async def test_an_unconfounded_turn_is_recorded_against_the_answering_model() -> None:
    """The base case — and the attribution AC the role name would have got wrong."""
    writer = AsyncMock()
    with patch("personal_agent.orchestrator.executor._write_compliance_observation", writer):
        status = _record_compliance_observation(
            _ctx(), _record(compliant=True), TurnEvidenceClass.CITABLE
        )
        await wait_for_background_tasks()

    assert status == "recorded"
    writer.assert_called_once()
    assert writer.call_args.kwargs["model_key"] == MODEL
    assert writer.call_args.kwargs["compliant"] is True
    assert writer.call_args.kwargs["trace_id"] == "trace-1284"


@pytest.mark.asyncio
async def test_a_non_compliant_turn_is_recorded_too() -> None:
    """The denominator is turns, not passes — recording only successes would read 1.0."""
    writer = AsyncMock()
    with patch("personal_agent.orchestrator.executor._write_compliance_observation", writer):
        _record_compliance_observation(_ctx(), _record(compliant=False), TurnEvidenceClass.CITABLE)
        await wait_for_background_tasks()

    assert writer.call_args.kwargs["compliant"] is False


@pytest.mark.asyncio
async def test_a_pre_forced_turn_is_never_written() -> None:
    """AC-2 at the seam: the exclusion is a row that does not exist, not a read-time filter.

    A row that is never written cannot be counted by mistake later; a filter can be
    forgotten by the next reader.
    """
    writer = AsyncMock()
    with patch("personal_agent.orchestrator.executor._write_compliance_observation", writer):
        status = _record_compliance_observation(
            _ctx(), _record(retrieval_forced=True), TurnEvidenceClass.CITABLE
        )
        await wait_for_background_tasks()

    assert status == "confounded"
    writer.assert_not_called()


@pytest.mark.asyncio
async def test_an_uncitable_turn_is_never_written() -> None:
    """ADR-0139 D1 AC-5, at this seam: uncitable is confounded on the same footing as
    pre-forced — the system offered nothing to cite from, which is not evidence about
    the model.
    """
    writer = AsyncMock()
    with patch("personal_agent.orchestrator.executor._write_compliance_observation", writer):
        status = _record_compliance_observation(_ctx(), _record(), TurnEvidenceClass.UNCITABLE)
        await wait_for_background_tasks()

    assert status == "confounded"
    writer.assert_not_called()


@pytest.mark.asyncio
async def test_a_turn_verification_could_not_run_on_is_never_written() -> None:
    """A denied budget or a broken extractor is not evidence about the model."""
    writer = AsyncMock()
    with patch("personal_agent.orchestrator.executor._write_compliance_observation", writer):
        # None, matching what _record_grounding passes when verification did not run —
        # the exclusion still has to hold via record.available, not via this argument.
        _record_compliance_observation(_ctx(), _record(available=False, compliant=False), None)
        await wait_for_background_tasks()

    writer.assert_not_called()


@pytest.mark.asyncio
async def test_a_turn_with_no_non_exempt_span_is_never_written() -> None:
    """D5's denominator is turns containing at least one non-exempt span."""
    writer = AsyncMock()
    with patch("personal_agent.orchestrator.executor._write_compliance_observation", writer):
        _record_compliance_observation(
            _ctx(), _record(non_exempt_count=0), TurnEvidenceClass.CITABLE
        )
        await wait_for_background_tasks()

    writer.assert_not_called()


@pytest.mark.asyncio
async def test_an_unattributable_turn_is_not_credited_to_anyone() -> None:
    """No model key means no observation, rather than an observation on a guessed key.

    Crediting a turn to a default model is worse than losing it: the metric gates
    promotion, so a misattributed compliant turn buys a promotion the model never earned.
    """
    writer = AsyncMock()
    with patch("personal_agent.orchestrator.executor._write_compliance_observation", writer):
        status = _record_compliance_observation(
            _ctx(model_key=None), _record(), TurnEvidenceClass.CITABLE
        )
        await wait_for_background_tasks()

    assert status == "unattributable"
    writer.assert_not_called()


@pytest.mark.asyncio
async def test_a_store_failure_never_reaches_the_turn() -> None:
    """The write is best-effort by design; the ERROR line is what makes a wave visible.

    Asserting on the log line rather than merely on "nothing raised": a patch that failed
    to bind would also raise nothing, and the test would pass while proving that the happy
    path is quiet. The failure has to be *observed* to have been exercised.
    """
    from personal_agent.orchestrator.executor import _write_compliance_observation

    with (
        patch(
            "personal_agent.service.database.AsyncSessionLocal",
            side_effect=RuntimeError("postgres is down"),
        ),
        patch("personal_agent.orchestrator.executor.log") as logger,
    ):
        await _write_compliance_observation(
            model_key=MODEL, compliant=True, trace_id="trace-1284", session_id="s"
        )

    logger.exception.assert_called_once()
    assert logger.exception.call_args.args[0] == "grounding_compliance_observation_write_failed"
    logger.info.assert_not_called()
