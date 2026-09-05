"""Tests for ADR-0142 D4a — pause credit, the creditable-pause limit, and the turn lifetime cap (FRE-1392).

Verifies:
- AC-1: a credited pause does not shrink the work budget.
- AC-2: crediting stops at the configured turn-wide limit.
- AC-3: the limit is turn-wide, not per-constraint.
- AC-4: the lifetime cap preempts a pause already waiting.
- AC-5: no probe here exceeds the lifetime cap.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.config import settings
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from personal_agent.telemetry.trace import TraceContext

_TRANSPORT = "personal_agent.transport.agui.transport"


def _ctx(**overrides: object) -> ExecutionContext:
    defaults: dict[str, object] = {
        "session_id": "s1",
        "trace_id": "t1",
        "user_message": "hi",
        "mode": Mode.NORMAL,
        "channel": Channel.CHAT,
    }
    defaults.update(overrides)
    return ExecutionContext(**defaults)  # type: ignore[arg-type]


def _patch_no_preference(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load(user_id: object, constraint: str, **_kw: object) -> None:
        return None

    monkeypatch.setattr(ex, "_load_constraint_preference", fake_load)


def _patch_answered_after_delay(
    monkeypatch: pytest.MonkeyPatch, decision: str, delay_seconds: float
) -> None:
    """A pause that is genuinely answered (user_choice) after `delay_seconds`."""
    import asyncio

    async def fake_push(**kwargs: object) -> dict[str, str]:
        await asyncio.sleep(delay_seconds)
        return {"decision": decision, "resolution": "user_choice"}

    async def fake_emit(**kwargs: object) -> None:
        return None

    monkeypatch.setattr(f"{_TRANSPORT}.register_and_push_constraint", fake_push)
    monkeypatch.setattr(f"{_TRANSPORT}.emit_constraint_resolved", fake_emit)


@pytest.mark.asyncio
class TestCreditTheWait:
    """AC-1: a credited pause must not shrink the work budget."""

    async def test_deadline_remaining_unchanged_across_a_credited_pause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-1: deadline_remaining after a pause of duration d ~= before."""
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 900)
        monkeypatch.setattr(settings, "orchestrator_turn_lifetime_seconds", 1800)
        monkeypatch.setattr(settings, "orchestrator_creditable_pause_limit", 3)
        _patch_no_preference(monkeypatch)
        _patch_answered_after_delay(monkeypatch, "continue_10", delay_seconds=0.3)

        ctx = _ctx()
        before = ex._turn_deadline_remaining(ctx)

        await ex._maybe_pause_for_constraint(
            session_id="s1",
            trace_id="t1",
            user_id=uuid4(),
            constraint="tool_iteration_limit",
            context="ctx",
            ctx=ctx,
        )

        after = ex._turn_deadline_remaining(ctx)
        assert after == pytest.approx(before, abs=0.15)


@pytest.mark.asyncio
class TestCreditablePauseLimit:
    """AC-2/AC-3: the creditable-pause limit is turn-wide, across every constraint."""

    async def test_credit_stops_at_the_limit_same_constraint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-2: N+1 pauses, none timing out -> credited total is N*d, not (N+1)*d."""
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 900)
        monkeypatch.setattr(settings, "orchestrator_turn_lifetime_seconds", 1800)
        monkeypatch.setattr(settings, "orchestrator_creditable_pause_limit", 3)
        _patch_no_preference(monkeypatch)
        _patch_answered_after_delay(monkeypatch, "continue_10", delay_seconds=0.2)

        ctx = _ctx()
        for _ in range(4):  # N + 1 = 4
            await ex._maybe_pause_for_constraint(
                session_id="s1",
                trace_id="t1",
                user_id=uuid4(),
                constraint="tool_iteration_limit",
                context="ctx",
                ctx=ctx,
            )

        assert ctx.pause_count == 4
        # 3 credited pauses at ~0.2s each; the 4th must not be credited.
        assert ctx.credited_pause_seconds == pytest.approx(0.6, abs=0.15)
        assert ctx.credited_pause_seconds < 0.8 - 0.15  # would be ~0.8 if all 4 credited

    async def test_limit_is_turn_wide_not_per_constraint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-3: N+1 pauses from N+1 different constraints still exhaust the credit."""
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 900)
        monkeypatch.setattr(settings, "orchestrator_turn_lifetime_seconds", 1800)
        monkeypatch.setattr(settings, "orchestrator_creditable_pause_limit", 3)
        _patch_no_preference(monkeypatch)
        _patch_answered_after_delay(monkeypatch, "compress_continue", delay_seconds=0.2)

        ctx = _ctx()
        constraints = [
            "tool_iteration_limit",
            "context_compression",
            "attachment_cost",
            "artifact_builder",
        ]  # 4 distinct constraints = N + 1 for the default limit of 3
        for constraint in constraints:
            await ex._maybe_pause_for_constraint(
                session_id="s1",
                trace_id="t1",
                user_id=uuid4(),
                constraint=constraint,  # type: ignore[arg-type]
                context="ctx",
                allow_preference=(constraint != "attachment_cost"),
                ctx=ctx,
            )

        assert ctx.pause_count == 4
        assert [r.constraint for r in ctx.constraint_resolutions] == constraints
        # An implementation keyed on constraint name would credit every one of
        # these (4 distinct names, each seeing pause_count==0 for itself) —
        # this must instead cap at 3 total, turn-wide.
        assert ctx.credited_pause_seconds == pytest.approx(0.6, abs=0.15)


@pytest.mark.asyncio
class TestLifetimeCapPreemption:
    """AC-4: the lifetime cap preempts a pause that is already waiting."""

    async def test_cap_preempts_a_pause_already_waiting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Less lifetime remaining than the pause's timeout, unanswered.

        Resolves to the safe default at the cap, and the turn ends there.
        """
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 900)
        monkeypatch.setattr(settings, "orchestrator_turn_lifetime_seconds", 1800)
        monkeypatch.setattr(settings, "constraint_pause_timeout_seconds", 180.0)
        _patch_no_preference(monkeypatch)

        captured_timeout: dict[str, float] = {}

        async def fake_push(**kwargs: object) -> dict[str, str]:
            captured_timeout["timeout_seconds"] = kwargs["timeout_seconds"]  # type: ignore[assignment]
            # Never answered -> the waiter's own timeout would fire at 180s;
            # the lifetime cap must bind first and force this resolution.
            return {"decision": "finish_now", "resolution": "timeout_default"}

        async def fake_emit(**kwargs: object) -> None:
            return None

        monkeypatch.setattr(f"{_TRANSPORT}.register_and_push_constraint", fake_push)
        monkeypatch.setattr(f"{_TRANSPORT}.emit_constraint_resolved", fake_emit)

        ctx = _ctx()
        # Only 5s of lifetime left, far less than the 180s pause timeout.
        ctx.turn_started_monotonic = time.monotonic() - (1800 - 5)

        result = await ex._maybe_pause_for_constraint(
            session_id="s1",
            trace_id="t1",
            user_id=uuid4(),
            constraint="tool_iteration_limit",
            context="ctx",
            ctx=ctx,
        )

        assert result == "finish_now"  # the safe default (last option)
        assert captured_timeout["timeout_seconds"] <= 5.0 + 0.05
        assert ctx.turn_stopped_early is True
        assert ctx.final_reply is not None

    async def test_lifetime_already_exhausted_skips_opening_a_pause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lifetime cap already reached before opening.

        No pause is opened at all — mirrors the existing FRE-973 deadline
        auto-decline precedent.
        """
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 900)
        monkeypatch.setattr(settings, "orchestrator_turn_lifetime_seconds", 1800)
        _patch_no_preference(monkeypatch)

        pushed = {"called": False}

        async def fake_push(**kwargs: object) -> dict[str, str]:
            pushed["called"] = True
            return {"decision": "continue_10", "resolution": "user_choice"}

        monkeypatch.setattr(f"{_TRANSPORT}.register_and_push_constraint", fake_push)

        ctx = _ctx()
        ctx.turn_started_monotonic = time.monotonic() - 1900  # already past 1800s

        result = await ex._maybe_pause_for_constraint(
            session_id="s1",
            trace_id="t1",
            user_id=uuid4(),
            constraint="tool_iteration_limit",
            context="ctx",
            ctx=ctx,
        )

        assert pushed["called"] is False
        assert result == "finish_now"
        assert result.resolution == "lifetime_cap_exceeded"
        assert ctx.turn_stopped_early is True
        assert ctx.pause_count == 0  # never opened -> not accounted as a pause


@pytest.mark.asyncio
class TestNoProbeExceedsLifetimeCap:
    """AC-5: no probe's total wall-clock exceeds orchestrator_turn_lifetime_seconds."""

    async def test_total_elapsed_across_limit_and_preemption_stays_under_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two quick answered pauses exhaust the limit, then a third is preempted."""
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 900)
        monkeypatch.setattr(settings, "orchestrator_turn_lifetime_seconds", 1.0)
        monkeypatch.setattr(settings, "orchestrator_creditable_pause_limit", 2)
        monkeypatch.setattr(settings, "constraint_pause_timeout_seconds", 180.0)
        _patch_no_preference(monkeypatch)

        import asyncio

        call_count = {"n": 0}

        async def fake_push(**kwargs: object) -> dict[str, str]:
            call_count["n"] += 1
            if call_count["n"] <= 2:
                # Two quick, genuinely answered pauses -- both under the limit.
                await asyncio.sleep(0.05)
                return {"decision": "continue_10", "resolution": "user_choice"}
            # The third rides out its (already lifetime-capped) timeout.
            await asyncio.sleep(float(kwargs["timeout_seconds"]))  # type: ignore[arg-type]
            return {"decision": "finish_now", "resolution": "timeout_default"}

        async def fake_emit(**kwargs: object) -> None:
            return None

        monkeypatch.setattr(f"{_TRANSPORT}.register_and_push_constraint", fake_push)
        monkeypatch.setattr(f"{_TRANSPORT}.emit_constraint_resolved", fake_emit)

        ctx = _ctx()
        start = time.monotonic()
        for _ in range(5):
            await ex._maybe_pause_for_constraint(
                session_id="s1",
                trace_id="t1",
                user_id=uuid4(),
                constraint="tool_iteration_limit",
                context="ctx",
                ctx=ctx,
            )
            if ctx.turn_stopped_early:
                break
        elapsed = time.monotonic() - start

        assert ctx.pause_count == 3
        # Only the first 2 (the limit) are credited; the 3rd is not.
        assert ctx.credited_pause_seconds == pytest.approx(0.1, abs=0.1)
        assert ctx.turn_stopped_early is True
        assert elapsed <= settings.orchestrator_turn_lifetime_seconds + 0.3


def _mock_session() -> object:
    from unittest.mock import MagicMock

    mock_session = MagicMock()
    mock_session.add_message = AsyncMock()
    mock_session.get_messages = AsyncMock(return_value=[])
    return mock_session


@pytest.mark.asyncio
class TestCallSitesHonorLifetimeStop:
    """AC-4's "the turn ends there".

    Once a pause preempts on the lifetime cap, the calling step must
    short-circuit to SYNTHESIS rather than granting a bonus, resuming tool
    dispatch, or routing back through another LLM_CALL.
    """

    async def test_tool_iteration_limit_site_stops_at_synthesis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A preempted pause at the iteration-limit site must not grant continue_10."""
        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 900)
        ctx = _ctx(
            session_id="sess-1",
            trace_id="trace-1",
            messages=[{"role": "assistant", "content": "x", "tool_calls": []}],
        )
        ctx.tool_iteration_count = ex._resolve_max_iterations(ctx)

        async def fake_pause(**kwargs: object) -> str:
            # Simulate the lifetime cap firing while this pause was in flight.
            ctx.turn_stopped_early = True
            ex._stop_turn_for_lifetime_cap(ctx)
            return "continue_10"  # even a would-be bonus must not be granted

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", fake_pause)
        trace_ctx = TraceContext.new_trace()

        result = await ex.step_tool_execution(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        assert result == TaskState.SYNTHESIS
        assert ctx.tool_iteration_bonus == 0  # continue_10 must not have been applied
        assert ctx.final_reply is not None

    async def test_llm_call_gate_stops_on_lifetime_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The work budget still has room, but the lifetime cap does not.

        The tighter bound must win.
        """
        from unittest.mock import MagicMock, patch

        monkeypatch.setattr(settings, "orchestrator_task_timeout_seconds", 900)
        monkeypatch.setattr(settings, "orchestrator_turn_lifetime_seconds", 100)
        ctx = _ctx(
            session_id="sess-2",
            trace_id="trace-2",
            messages=[{"role": "user", "content": "hi"}],
        )
        ctx.turn_started_monotonic = time.monotonic() - 200  # past the 100s lifetime,
        # but well inside the 900s work budget (credited_pause_seconds=0 here).

        respond_mock = AsyncMock()
        mock_llm = MagicMock()
        mock_llm.respond = respond_mock
        mock_llm.model_configs = {}
        trace_ctx = TraceContext.new_trace()

        with (
            patch("personal_agent.orchestrator.skills.get_skill_bodies", return_value=("", ())),
            patch("personal_agent.orchestrator.skills.assemble_skill_index", return_value=""),
            patch(
                "personal_agent.orchestrator.skills.assemble_skill_index_directive",
                return_value="",
            ),
            patch(
                "personal_agent.orchestrator.skills.assemble_skill_usage_directives",
                return_value="",
            ),
            patch("personal_agent.orchestrator.skills.get_all_skills", return_value={}),
            patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_llm),
            patch(
                "personal_agent.orchestrator.executor.get_default_registry",
                return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
            ),
        ):
            result = await ex.step_llm_call(ctx, _mock_session(), trace_ctx)  # type: ignore[arg-type]

        assert result == TaskState.SYNTHESIS
        assert not respond_mock.called
        assert ctx.final_reply is not None
        assert "lifetime cap" in ctx.final_reply.lower()
