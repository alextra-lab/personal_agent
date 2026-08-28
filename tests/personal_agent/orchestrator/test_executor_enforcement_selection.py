"""D5 enforcement selection wired into the turn path (ADR-0138 D5, FRE-1285).

The pure state machine lives in ``tests/personal_agent/grounding/test_enforcement_selection``.
These tests drive the seams that turn a decision into a turn: whether heavy actually gates
retrieval before generation, whether a probation turn reaches the compliance denominator,
whether verification is the same at both levels, and whether every failure lands on heavy.

Three of these exist because a plan review pointed out that the first draft's criteria
could all pass while the real behaviour was broken — a directive the model may ignore is
not a gate, and a probation turn that reports itself pre-forced is discarded by the very
metric it was routed light to feed.
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
from personal_agent.llm_client.models import ToolCallingStrategy
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.executor import (
    _record_grounding,
    _resolve_heavy_gate,
    _select_enforcement,
)
from personal_agent.orchestrator.types import ExecutionContext

MODEL = "gemma-3-27b-it-qat"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
TOOLS = [{"type": "function", "function": {"name": "search_web"}}]


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


# ── AC-6b — heavy actually gates retrieval before generation ─────────────────


def test_heavy_pins_tool_choice_required() -> None:
    """The gate, stated as the thing that makes heavy more than advice (AC-6b)."""
    ctx = _ctx(grounding_enforcement=_selection(EnforcementLevel.HEAVY))
    pin = _resolve_heavy_gate(
        ctx,
        tools=TOOLS,
        tool_strategy=ToolCallingStrategy.NATIVE,
        is_synthesizing=False,
        model_key=MODEL,
    )
    assert pin == "required"


def test_light_leaves_tool_choice_alone() -> None:
    """Light means the model generates first and cites as it goes (AC-6b)."""
    ctx = _ctx(grounding_enforcement=_selection(EnforcementLevel.LIGHT))
    assert (
        _resolve_heavy_gate(
            ctx,
            tools=TOOLS,
            tool_strategy=ToolCallingStrategy.NATIVE,
            is_synthesizing=False,
            model_key=MODEL,
        )
        is None
    )


def test_probation_turn_carries_no_gate() -> None:
    """AC-4b: probation withholds the forcing, which is the entire point of it.

    A probation turn that still gated retrieval would be a heavy turn wearing a light
    label — measurable in name, confounded in fact.
    """
    ctx = _ctx(
        grounding_enforcement=_selection(
            EnforcementLevel.LIGHT, standing=EnforcementLevel.HEAVY, probation=True
        )
    )
    assert (
        _resolve_heavy_gate(
            ctx,
            tools=TOOLS,
            tool_strategy=ToolCallingStrategy.NATIVE,
            is_synthesizing=False,
            model_key=MODEL,
        )
        is None
    )


def test_the_gate_applies_only_to_the_first_generation() -> None:
    """Re-pinning every pass would forbid the turn from ever answering."""
    mid_loop = _ctx(
        grounding_enforcement=_selection(EnforcementLevel.HEAVY), tool_iteration_count=1
    )
    assert (
        _resolve_heavy_gate(
            mid_loop,
            tools=TOOLS,
            tool_strategy=ToolCallingStrategy.NATIVE,
            is_synthesizing=False,
            model_key=MODEL,
        )
        is None
    )

    after_d4_retry = _ctx(
        grounding_enforcement=_selection(EnforcementLevel.HEAVY), grounding_attempts=1
    )
    assert (
        _resolve_heavy_gate(
            after_d4_retry,
            tools=TOOLS,
            tool_strategy=ToolCallingStrategy.NATIVE,
            is_synthesizing=False,
            model_key=MODEL,
        )
        is None
    )


@pytest.mark.parametrize(
    ("tools", "strategy", "synthesizing"),
    [
        (None, ToolCallingStrategy.NATIVE, False),
        (TOOLS, ToolCallingStrategy.PROMPT_INJECTED, False),
        (TOOLS, ToolCallingStrategy.NATIVE, True),
    ],
)
def test_gate_unavailable_degrades_loudly(tools, strategy, synthesizing) -> None:
    """AC-6b: an unreachable gate is a WARNING, never a silent downgrade.

    These are exactly the conditions under which ``tool_choice`` never reaches a backend.
    Heavy then falls back to directive-only — which is the design a plan review rejected,
    so a deployment sitting in it must be visible from the logs.
    """
    ctx = _ctx(grounding_enforcement=_selection(EnforcementLevel.HEAVY))
    with patch("personal_agent.orchestrator.executor.log") as logger:
        pin = _resolve_heavy_gate(
            ctx,
            tools=tools,
            tool_strategy=strategy,
            is_synthesizing=synthesizing,
            model_key=MODEL,
        )

    assert pin is None
    logger.warning.assert_called_once()
    assert logger.warning.call_args.args[0] == "grounding_heavy_gate_unavailable"


def test_the_gate_never_overrides_forced_synthesis() -> None:
    """Forced synthesis pins ``tool_choice="none"``; the gate must not fight it.

    A synthesis pass told it must call a tool cannot synthesize, and the turn would never
    produce an answer at all.
    """
    ctx = _ctx(grounding_enforcement=_selection(EnforcementLevel.HEAVY))
    assert (
        _resolve_heavy_gate(
            ctx,
            tools=TOOLS,
            tool_strategy=ToolCallingStrategy.NATIVE,
            is_synthesizing=True,
            model_key=MODEL,
        )
        is None
    )


# ── AC-4b / AC-5 — what the metric is told about the turn ────────────────────


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


def test_a_heavy_turn_is_recorded_as_pre_forced() -> None:
    """AC-5's root cause, closed at the seam.

    Heavy supplies sources before generation, so scoring the turn measures the
    enforcement rather than the model. A heavy turn counted as unforced is how a model
    that only complies when spoon-fed earns promotion and then oscillates forever.
    """
    record = _record_with(_selection(EnforcementLevel.HEAVY))
    assert record.retrieval_forced is True


def test_a_probation_turn_is_an_unconfounded_observation() -> None:
    """AC-4b: probation reaches the denominator, or the bootstrap deadlocks silently.

    ``retrieval_forced`` reads the APPLIED level, never the standing one. A probation
    turn reporting itself forced would be discarded by the very metric it was routed
    light to feed, and the model could never accrue what promotion requires — with every
    piece of machinery apparently working.
    """
    record = _record_with(
        _selection(EnforcementLevel.LIGHT, standing=EnforcementLevel.HEAVY, probation=True)
    )
    assert record.retrieval_forced is False


def test_a_light_turn_is_recorded_as_unforced() -> None:
    """The measurable case: light turns are the population the rate is computed over."""
    assert _record_with(_selection(EnforcementLevel.LIGHT)).retrieval_forced is False


def test_a_d4_retry_is_still_pre_forced_under_light() -> None:
    """The field's original meaning survives the widening (FRE-1282)."""
    record = _record_with(_selection(EnforcementLevel.LIGHT), attempts=2)
    assert record.retrieval_forced is True


def test_no_selection_leaves_the_field_at_its_pre_1285_meaning() -> None:
    """`observe` mode never selects, and must go on producing observations."""
    assert _record_with(None).retrieval_forced is False


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
    """AC-6: the contract does not vary; only pre-generation forcing does.

    Seeded identically at each level and asserted on the outcome that matters — the span
    fails, so the turn is not compliant and D4 blocks it. A level that admitted it would
    be a second, weaker contract.
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
async def test_observe_mode_never_selects_or_forces() -> None:
    """Forcing retrieval in a mode that promises not to change behaviour is a lie.

    `observe` is also where every turn is unconfounded, which is the bootstrap the mode
    exists to provide.
    """
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


@pytest.mark.asyncio
async def test_heavy_attaches_the_directive_and_the_iteration_grant() -> None:
    """Heavy's other half: the gate makes retrieval happen, this says what for.

    The grant matters independently — a turn that already spent its tool budget would
    otherwise be told to retrieve with no iteration left to retrieve with, which is a
    refusal caused by our accounting rather than by the absence of a source.
    """
    ctx = _ctx()
    resolver = AsyncMock(return_value=_selection(EnforcementLevel.HEAVY))
    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor._resolve_enforcement", resolver),
    ):
        cfg.grounding_verification_mode = "enforce"
        await _select_enforcement(ctx)

    assert ctx.grounding_enforcement is not None
    assert ctx.grounding_enforcement.applied is EnforcementLevel.HEAVY
    assert len(ctx.messages) == 1
    assert "retriev" in ctx.messages[0]["content"].lower()
    assert ctx.grounding_retrieval_grant > 0


@pytest.mark.asyncio
async def test_light_attaches_nothing() -> None:
    """Light leaves the turn alone: the model generates first and cites as it goes."""
    ctx = _ctx()
    resolver = AsyncMock(return_value=_selection(EnforcementLevel.LIGHT))
    with (
        patch("personal_agent.orchestrator.executor.settings") as cfg,
        patch("personal_agent.orchestrator.executor._resolve_enforcement", resolver),
    ):
        cfg.grounding_verification_mode = "enforce"
        await _select_enforcement(ctx)

    assert ctx.messages == []
    assert ctx.grounding_retrieval_grant == 0


@pytest.mark.asyncio
async def test_a_store_failure_falls_back_to_heavy() -> None:
    """Unmeasured means heavy, and a broken instrument is no better than no instrument.

    Asserted on the whole fail-safe, not just the level: the turn still runs, it runs
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
    assert ctx.grounding_enforcement.retrieval_forced
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


@pytest.mark.asyncio
async def test_a_transition_is_persisted_before_the_turn_proceeds() -> None:
    """The write is awaited, not backgrounded.

    A lost demotion is the one loss no later turn repairs: the next turn re-demotes with
    a LATER stamp, handing the model a cooldown it has already partly served.
    """
    from personal_agent.orchestrator.executor import _resolve_enforcement

    band = EnforcementBand(
        promote_at=0.95, demote_below=0.90, cooldown=timedelta(hours=24), probation_rate=0.0
    )
    recorded_upsert = AsyncMock(return_value=True)

    class _Compliance:
        def __init__(self, db):
            pass

        async def recent(self, model_key, *, limit):
            return []

    class _Enforcement:
        upsert = recorded_upsert

        def __init__(self, db):
            pass

        async def get(self, model_key):
            return EnforcementState(level=EnforcementLevel.LIGHT, demoted_at=None)

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
        selection = await _resolve_enforcement(MODEL, band=band, now=NOW)

    # No observations → unmeasured → heavy, and from LIGHT that is a demotion that stamps
    # the cooldown. The stamp is the thing that had to reach the database.
    assert selection.applied is EnforcementLevel.HEAVY
    assert selection.standing.demoted_at == NOW
    assert selection.changed
    recorded_upsert.assert_awaited_once()
    assert recorded_upsert.await_args.kwargs["updated_at"] == NOW
