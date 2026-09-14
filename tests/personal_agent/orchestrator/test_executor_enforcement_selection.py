"""D5 enforcement selection wired into the turn path (ADR-0138 D5, FRE-1285).

The pure state machine lives in ``tests/personal_agent/grounding/test_enforcement_selection``.
These tests drive the seams where a selection meets a turn: whether verification is the
same at every level, whether every failure lands on heavy, and how a transition is
persisted.

ADR-0151 D5 (FRE-1509) withdrew pre-generation forcing. The selector is retained but inert:
a heavy selection attaches no directive, no ``tool_choice`` pin and no iteration grant, and
``retrieval_forced`` reads the attempt count only. The request-level proof is in
``test_executor_cite_only_retry.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.captains_log.turn_evidence import GroundingRecord
from personal_agent.governance.models import Mode
from personal_agent.grounding.enforcement_selection import (
    EnforcementBand,
    EnforcementLevel,
    EnforcementSelection,
    EnforcementState,
    SelectionReason,
)
from personal_agent.grounding.verification import (
    CheckOutcome,
    SpanVerification,
    TurnVerification,
)
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.executor import (
    _record_grounding,
    _select_enforcement,
)
from personal_agent.orchestrator.types import ExecutionContext

MODEL = "gemma-3-27b-it-qat"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def _ctx(**kwargs: object) -> ExecutionContext:
    ctx = ExecutionContext(
        trace_id="trace-1285",
        session_id="session-enforcement",
        user_message="How many people live in Paris?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    ctx.answering_model_key = MODEL
    for key, value in kwargs.items():
        setattr(ctx, key, value)
    return ctx


def _selection(
    applied: EnforcementLevel,
    *,
    standing: EnforcementLevel | None = None,
    probation: bool = False,
) -> EnforcementSelection:
    return EnforcementSelection(
        applied=applied,
        standing=EnforcementState(level=standing or applied, demoted_at=None),
        reason=SelectionReason.BAND_HOLD,
        probation=probation,
    )


# ── ADR-0151 D5 — what the metric is told about the turn ─────────────────────


def _verification(*, compliant: bool) -> TurnVerification:
    span = SpanVerification(
        text="Paris has 2.1 million residents.",
        start=0,
        end=32,
        identifier="S1@a3f91c2b7d4e6f80" if compliant else None,
        outcome=CheckOutcome.PASSED if compliant else CheckOutcome.UNCITED,
        detail="ok" if compliant else "assertion carried no citation",
    )
    return TurnVerification(available=True, spans=[span])


def _record_with(selection: EnforcementSelection | None, *, attempts: int = 1) -> GroundingRecord:
    ctx = _ctx(grounding_enforcement=selection, grounding_attempts=attempts)
    with patch("personal_agent.orchestrator.executor._record_compliance_observation") as observer:
        observer.return_value = "recorded"
        _record_grounding(ctx, _verification(compliant=True), "enforce")
    assert ctx.grounding_record is not None
    return ctx.grounding_record


@pytest.mark.parametrize(
    "selection",
    [
        None,
        _selection(EnforcementLevel.LIGHT),
        _selection(EnforcementLevel.HEAVY),
        _selection(EnforcementLevel.LIGHT, standing=EnforcementLevel.HEAVY, probation=True),
    ],
)
def test_a_first_attempt_is_never_recorded_as_forced(
    selection: EnforcementSelection | None,
) -> None:
    """ADR-0151 D5: the selection level never sets ``retrieval_forced``.

    A heavy turn recorded as forced would be excluded from the compliance metric although
    nothing forced it, and the metric would starve.
    """
    assert _record_with(selection).retrieval_forced is False


@pytest.mark.parametrize(
    "selection", [None, _selection(EnforcementLevel.LIGHT), _selection(EnforcementLevel.HEAVY)]
)
def test_a_retried_attempt_is_recorded_as_forced_at_every_level(
    selection: EnforcementSelection | None,
) -> None:
    """``retrieval_forced`` is true exactly when ``attempts >= 2``."""
    assert _record_with(selection, attempts=2).retrieval_forced is True


# ── AC-6 — verification is identical at both levels ──────────────────────────


@pytest.mark.parametrize(
    "selection",
    [
        _selection(EnforcementLevel.LIGHT),
        _selection(EnforcementLevel.HEAVY),
        _selection(EnforcementLevel.LIGHT, standing=EnforcementLevel.HEAVY, probation=True),
    ],
)
def test_the_same_bad_citation_is_blocked_at_every_level(
    selection: EnforcementSelection,
) -> None:
    """AC-6: the contract does not vary with the level.

    Seeded identically at each level and asserted on the outcome that matters — the span
    fails, so the turn is not compliant. A level that admitted it would be a second,
    weaker contract.
    """
    ctx = _ctx(grounding_enforcement=selection, grounding_attempts=1)
    with patch("personal_agent.orchestrator.executor._record_compliance_observation") as observer:
        observer.return_value = "recorded"
        _record_grounding(ctx, _verification(compliant=False), "enforce")

    record = ctx.grounding_record
    assert record is not None
    assert record.first_generation_compliant is False
    assert record.no_source_count == 1
    assert record.passed_count == 0


# ── Selection in the turn path ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_observe_mode_never_selects() -> None:
    """`observe` promises not to change behaviour, and never runs the selector."""
    ctx = _ctx()
    with patch("personal_agent.orchestrator.executor.settings") as cfg:
        cfg.grounding_verification_mode = "observe"
        await _select_enforcement(ctx)

    assert ctx.grounding_enforcement is None
    assert ctx.messages == []
    assert ctx.grounding_retrieval_grant == 0


@pytest.mark.asyncio
async def test_selection_happens_once_per_turn() -> None:
    """The level describes how the turn was generated, not how its last pass would be."""
    ctx = _ctx(grounding_enforcement=_selection(EnforcementLevel.LIGHT))
    resolver = AsyncMock()
    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor._resolve_enforcement", resolver),
    ):
        cfg.grounding_verification_mode = "enforce"
        await _select_enforcement(ctx)

    resolver.assert_not_called()


@pytest.mark.parametrize("level", [EnforcementLevel.HEAVY, EnforcementLevel.LIGHT])
@pytest.mark.asyncio
async def test_a_selection_changes_nothing_on_the_turn(level: EnforcementLevel) -> None:
    """ADR-0151 D5: no directive in history and no iteration grant, at either level."""
    ctx = _ctx()
    resolver = AsyncMock(return_value=_selection(level))
    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor._resolve_enforcement", resolver),
    ):
        cfg.grounding_verification_mode = "enforce"
        await _select_enforcement(ctx)

    assert ctx.grounding_enforcement is not None
    assert ctx.grounding_enforcement.applied is level
    assert ctx.messages == []
    assert ctx.grounding_retrieval_grant == 0


@pytest.mark.asyncio
async def test_a_store_failure_falls_back_to_heavy() -> None:
    """Unmeasured means heavy, and a broken instrument is no better than no instrument.

    Asserted on the whole fail-safe, not just the level: the turn still runs, it selects
    heavy, and the failure is logged at ERROR so a wave of these reads as the malfunction
    it is rather than as models quietly becoming strict.
    """
    ctx = _ctx()
    resolver = AsyncMock(side_effect=RuntimeError("postgres is down"))
    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor._resolve_enforcement", resolver),
        patch("personal_agent.orchestrator.executor.log") as logger,
    ):
        cfg.grounding_verification_mode = "enforce"
        await _select_enforcement(ctx)

    assert ctx.grounding_enforcement is not None
    assert ctx.grounding_enforcement.applied is EnforcementLevel.HEAVY
    logger.exception.assert_called_once()
    assert logger.exception.call_args.args[0] == "grounding_enforcement_selection_failed"


@pytest.mark.asyncio
async def test_a_turn_with_no_model_key_falls_back_to_heavy() -> None:
    """No key means no history to read, and no history means heavy."""
    ctx = _ctx(answering_model_key=None)
    with patch("personal_agent.orchestrator.executor.settings") as cfg:
        cfg.grounding_verification_mode = "enforce"
        await _select_enforcement(ctx)

    assert ctx.grounding_enforcement is not None
    assert ctx.grounding_enforcement.applied is EnforcementLevel.HEAVY


@pytest.mark.asyncio
async def test_a_failed_selection_is_never_persisted() -> None:
    """A reading we could not take must not overwrite the one we have.

    The fail-safe selection is heavy with a *default* standing state; persisting it would
    clear a real cooldown stamp because Postgres was briefly unreachable.
    """
    ctx = _ctx()
    resolver = AsyncMock(side_effect=RuntimeError("postgres is down"))
    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor._resolve_enforcement", resolver),
    ):
        cfg.grounding_verification_mode = "enforce"
        await _select_enforcement(ctx)

    assert ctx.grounding_enforcement is not None
    assert not ctx.grounding_enforcement.changed


BAND = EnforcementBand(
    promote_at=0.95, demote_below=0.90, cooldown=timedelta(hours=24), probation_rate=0.0
)


async def _run_resolve(*, upsert: AsyncMock, read_at: list[datetime] | None = None):
    """Drive ``_resolve_enforcement`` against stubbed repositories.

    Args:
        upsert: The stub the enforcement repository's write is recorded on.
        read_at: When non-None, appended to with the wall-clock instant of the read, so a
            test can assert the write's ``updated_at`` was taken afterwards.

    Returns:
        The selection.
    """
    from personal_agent.orchestrator.executor import _resolve_enforcement

    class _Compliance:
        def __init__(self, db):
            pass

        async def recent(self, model_key, *, limit):
            if read_at is not None:
                read_at.append(datetime.now(timezone.utc))
            return []

    class _Enforcement:
        def __init__(self, db):
            pass

        async def get(self, model_key):
            return EnforcementState(level=EnforcementLevel.LIGHT, demoted_at=None)

    _Enforcement.upsert = upsert

    with (
        patch(
            "personal_agent.service.repositories.grounding_compliance_repository."
            "GroundingComplianceRepository",
            _Compliance,
        ),
        patch(
            "personal_agent.service.repositories.grounding_enforcement_repository."
            "GroundingEnforcementRepository",
            _Enforcement,
        ),
        patch("personal_agent.service.database.AsyncSessionLocal"),
    ):
        return await _resolve_enforcement(_ctx(), MODEL, band=BAND)


@pytest.mark.asyncio
async def test_a_transition_is_persisted_before_the_turn_proceeds() -> None:
    """The write is awaited, not backgrounded.

    A lost demotion is the one loss no later turn repairs: the next turn re-demotes with
    a LATER stamp, handing the model a cooldown it has already partly served.
    """
    upsert = AsyncMock(return_value=True)
    selection = await _run_resolve(upsert=upsert)

    # No observations → unmeasured → heavy, and from LIGHT that is a demotion that stamps
    # the cooldown. The stamp is the thing that had to reach the database.
    assert selection.applied is EnforcementLevel.HEAVY
    assert selection.standing.demoted_at is not None
    assert selection.changed
    upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_write_timestamp_is_taken_after_the_read() -> None:
    """The guard orders writers by this value, so it must date the state observed.

    Taking it before the read means a turn that waited on the connection pool
    (``pool_timeout=30``) carries a stamp older than a turn that read *later* — so it wins
    the guard while holding the staler view, and erases a demotion's cooldown. The whole
    fix is which side of the read this instant is captured on, so that is what is
    asserted.
    """
    upsert = AsyncMock(return_value=True)
    read_at: list[datetime] = []

    selection = await _run_resolve(upsert=upsert, read_at=read_at)

    assert read_at, "the read stub never ran"
    written_at = upsert.await_args.kwargs["updated_at"]
    assert written_at >= read_at[0]
    # And the same instant dates the cooldown stamp, so the row is self-consistent.
    assert selection.standing.demoted_at == written_at


@pytest.mark.asyncio
async def test_a_rejected_write_is_logged_rather_than_swallowed() -> None:
    """A discarded transition must not be reported as a landed one.

    The repository returns whether the row now reflects this state. Dropping that value
    while the selection log line says ``changed=True`` makes the telemetry assert a
    transition that is not in the store — and on a demotion, a missing cooldown stamp that
    nothing downstream could detect.
    """
    upsert = AsyncMock(return_value=False)

    with patch("personal_agent.orchestrator.executor.log") as logger:
        selection = await _run_resolve(upsert=upsert)

    # The selection still governs THIS turn — the loser of the race is the write, not the
    # enforcement decision.
    assert selection.applied is EnforcementLevel.HEAVY
    logger.warning.assert_called_once()
    assert logger.warning.call_args.args[0] == "grounding_enforcement_transition_not_persisted"


@pytest.mark.asyncio
async def test_a_landed_write_logs_no_warning() -> None:
    """The seeded positive: the warning must distinguish, not fire on every transition."""
    upsert = AsyncMock(return_value=True)

    with patch("personal_agent.orchestrator.executor.log") as logger:
        await _run_resolve(upsert=upsert)

    warned = [c for c in logger.warning.call_args_list if c.args]
    assert not any(c.args[0] == "grounding_enforcement_transition_not_persisted" for c in warned)
