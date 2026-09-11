"""ADR-0149 D4 (FRE-1484) — the caller cannot hide a failed landing.

An incomplete sub-agent fan-out must pause in front of the owner before the
primary ever synthesizes a confident answer over it. ``stop_and_show`` makes
no model call; ``answer_from_partial`` carries a deterministic trailer the
model cannot remove; a headless eval caller never waits on the pause.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.constraint_options import ConstraintDecision
from personal_agent.orchestrator.expansion_controller import ExpansionResult
from personal_agent.orchestrator.sub_agent_types import SubAgentResult
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from personal_agent.request_gateway.types import (
    AssembledContext,
    Complexity,
    DecompositionResult,
    DecompositionStrategy,
    GatewayOutput,
    GovernanceContext,
    IntentResult,
    TaskType,
)
from personal_agent.telemetry.trace import TraceContext


def _sub_result(
    task: str = "task_0",
    *,
    success: bool = True,
    stop_reason: str = "completed",
    report_kind: str = "synthesized",
    full_output: str = "the findings",
    tool_iterations: int = 3,
    tool_result_chars_absorbed: int = 1000,
) -> SubAgentResult:
    return SubAgentResult(
        task_id=uuid4(),
        spec_task=task,
        summary=full_output,
        full_output=full_output,
        tools_used=[],
        token_count=10,
        duration_ms=1.0,
        success=success,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        report_kind=report_kind,  # type: ignore[arg-type]
        tool_iterations=tool_iterations,
        tool_result_chars_absorbed=tool_result_chars_absorbed,
    )


def _gateway_output(eval_mode: bool = False) -> GatewayOutput:
    return GatewayOutput(
        intent=IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        ),
        governance=GovernanceContext(mode=Mode.NORMAL, expansion_permitted=True),
        decomposition=DecompositionResult(
            strategy=DecompositionStrategy.HYBRID,
            reason="test",
            constraints={"max_sub_agents": 2},
        ),
        context=AssembledContext(
            messages=[{"role": "user", "content": "research this"}],
            memory_context=None,
            tool_definitions=None,
        ),
        session_id="s1",
        trace_id="t1",
    )


def _ctx(eval_mode: bool = False) -> ExecutionContext:
    return ExecutionContext(
        session_id="s1",
        trace_id="t1",
        user_message="research this",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        gateway_output=_gateway_output(),
        eval_mode=eval_mode,
        user_id=uuid4(),
    )


def _session_manager() -> MagicMock:
    session_manager = MagicMock()
    session_manager.get_session = MagicMock(return_value=None)
    return session_manager


def _patch_expansion(monkeypatch: pytest.MonkeyPatch, exp_result: ExpansionResult) -> AsyncMock:
    controller = MagicMock()
    controller.execute = AsyncMock(return_value=exp_result)
    monkeypatch.setattr(
        "personal_agent.orchestrator.expansion_controller.ExpansionController",
        lambda: controller,
    )
    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client",
        lambda role_name=None: MagicMock(),
    )
    return controller.execute


class TestFanoutIncompleteTasks:
    """Pure predicate — no pause/decision machinery involved."""

    def test_complete_fanout_is_empty(self) -> None:
        results = [_sub_result("a"), _sub_result("b")]
        assert ex._fanout_incomplete_tasks(results, []) == []

    def test_failed_success_is_incomplete(self) -> None:
        """Fixture A — stopped at the cap with a synthesized (partial) report."""
        results = [_sub_result("a", success=False, stop_reason="cap")]
        assert ex._fanout_incomplete_tasks(results, []) == [("a", "cap")]

    def test_completed_with_ledger_is_incomplete(self) -> None:
        """Fixture B — completed but the content is a bare ledger, no report."""
        results = [_sub_result("a", success=False, stop_reason="completed", report_kind="ledger")]
        assert ex._fanout_incomplete_tasks(results, []) == [("a", "completed")]

    def test_skipped_task_is_incomplete(self) -> None:
        assert ex._fanout_incomplete_tasks([], ["never_dispatched"]) == [
            ("never_dispatched", "not_dispatched")
        ]


class TestFanoutTrailerText:
    def test_names_task_and_stop_reason(self) -> None:
        results = [
            _sub_result("a", success=True),
            _sub_result("b", success=False, stop_reason="cap"),
        ]
        trailer = ex._fanout_trailer_text(results, [])
        assert "1 of 2 sub-tasks did not complete" in trailer
        assert "b: cap" in trailer
        assert "not verified by this turn's research" in trailer


class TestComposeStopAndShow:
    def test_contains_every_report_and_stop_reason(self) -> None:
        results = [
            _sub_result("a", success=False, stop_reason="cap", full_output="MARKER_A"),
            _sub_result(
                "b",
                success=False,
                stop_reason="deadline",
                report_kind="ledger",
                full_output="MARKER_B",
            ),
        ]
        response = ex._compose_fanout_stop_and_show(results, [])
        assert "MARKER_A" in response
        assert "MARKER_B" in response
        assert "stop=cap" in response
        assert "stop=deadline" in response


class TestStepInitPausesOnIncompleteFanout:
    """AC-1 — an incomplete fan-out cannot reach synthesis unpaused."""

    @pytest.mark.asyncio
    async def test_fixture_a_cap_synthesized_pauses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[
                _sub_result("a", success=False, stop_reason="cap", report_kind="synthesized")
            ],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        pause_mock = AsyncMock(return_value=ConstraintDecision("stop_and_show", "user_choice"))
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", pause_mock)

        ctx = _ctx()
        await ex.step_init(ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1"))

        pause_mock.assert_awaited_once()
        assert pause_mock.await_args.kwargs["constraint"] == "sub_agent_fanout_incomplete"

    @pytest.mark.asyncio
    async def test_fixture_b_completed_ledger_pauses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[
                _sub_result("a", success=False, stop_reason="completed", report_kind="ledger")
            ],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        pause_mock = AsyncMock(return_value=ConstraintDecision("stop_and_show", "user_choice"))
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", pause_mock)

        ctx = _ctx()
        await ex.step_init(ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1"))

        pause_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fixture_c_all_successful_no_pause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[_sub_result("a", success=True), _sub_result("b", success=True)],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        pause_mock = AsyncMock(return_value=ConstraintDecision("stop_and_show", "user_choice"))
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", pause_mock)

        ctx = _ctx()
        state = await ex.step_init(
            ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1")
        )

        pause_mock.assert_not_called()
        assert state == TaskState.LLM_CALL
        assert ctx.fanout_trailer is None


class TestStopAndShowMakesNoModelCall:
    """AC-2 — stop_and_show makes no model call and shows every report."""

    @pytest.mark.asyncio
    async def test_stop_and_show_returns_synthesis_with_full_reports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[
                _sub_result("a", success=False, stop_reason="cap", full_output="MARKER_REPORT_A")
            ],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        monkeypatch.setattr(
            ex,
            "_maybe_pause_for_constraint",
            AsyncMock(return_value=ConstraintDecision("stop_and_show", "user_choice")),
        )

        ctx = _ctx()
        state = await ex.step_init(
            ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1")
        )

        assert state == TaskState.SYNTHESIS
        assert ctx.final_reply is not None
        assert "MARKER_REPORT_A" in ctx.final_reply
        assert "stop=cap" in ctx.final_reply
        # No synthesis message was queued for a further LLM call.
        assert not any(
            m.get("role") == "user" and "Synthesize the results" in str(m.get("content", ""))
            for m in ctx.messages
        )


class TestAnswerFromPartialCarriesTrailer:
    """AC-3 — the trailer is present and names the tasks."""

    @pytest.mark.asyncio
    async def test_trailer_set_on_answer_from_partial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[_sub_result("a", success=False, stop_reason="cap")],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        monkeypatch.setattr(
            ex,
            "_maybe_pause_for_constraint",
            AsyncMock(return_value=ConstraintDecision("answer_from_partial", "user_choice")),
        )

        ctx = _ctx()
        state = await ex.step_init(
            ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1")
        )

        assert state == TaskState.LLM_CALL
        assert ctx.fanout_trailer is not None
        assert "a: cap" in ctx.fanout_trailer

    @pytest.mark.asyncio
    async def test_no_trailer_on_complete_fanout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[_sub_result("a", success=True)],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        pause_mock = AsyncMock()
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", pause_mock)

        ctx = _ctx()
        await ex.step_init(ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1"))

        assert ctx.fanout_trailer is None
        pause_mock.assert_not_called()


class TestEvalModeNeverWaits:
    """AC-4 — an eval fan-out never waits on the pause timeout."""

    @pytest.mark.asyncio
    async def test_no_preference_resolves_stop_and_show_with_no_pause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[_sub_result("a", success=False, stop_reason="cap")],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        pause_mock = AsyncMock()
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", pause_mock)
        monkeypatch.setattr(ex, "_load_constraint_preference", AsyncMock(return_value=None))

        ctx = _ctx(eval_mode=True)
        state = await ex.step_init(
            ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1")
        )

        pause_mock.assert_not_called()
        assert state == TaskState.SYNTHESIS
        assert ctx.final_reply is not None

    @pytest.mark.asyncio
    async def test_always_pause_preference_resolves_stop_and_show_with_no_pause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[_sub_result("a", success=False, stop_reason="cap")],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        pause_mock = AsyncMock()
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", pause_mock)
        monkeypatch.setattr(
            ex, "_load_constraint_preference", AsyncMock(return_value="always_pause")
        )

        ctx = _ctx(eval_mode=True)
        state = await ex.step_init(
            ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1")
        )

        pause_mock.assert_not_called()
        assert state == TaskState.SYNTHESIS

    @pytest.mark.asyncio
    async def test_answer_from_partial_preference_proceeds_to_synthesis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[_sub_result("a", success=False, stop_reason="cap")],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        pause_mock = AsyncMock()
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", pause_mock)
        monkeypatch.setattr(
            ex, "_load_constraint_preference", AsyncMock(return_value="answer_from_partial")
        )

        ctx = _ctx(eval_mode=True)
        state = await ex.step_init(
            ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1")
        )

        pause_mock.assert_not_called()
        assert state == TaskState.LLM_CALL
        assert ctx.fanout_trailer is not None


class TestSeededNegativeD4Pause:
    """AC-6 — disabling the D4 pause must fail AC-1: an incomplete fan-out
    reaches synthesis unpaused.
    """

    @pytest.mark.asyncio
    async def test_disabling_the_predicate_lets_incomplete_fanout_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[_sub_result("a", success=False, stop_reason="cap")],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        pause_mock = AsyncMock()
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", pause_mock)
        # The disabled mechanism: the predicate always reports nothing incomplete.
        monkeypatch.setattr(ex, "_fanout_incomplete_tasks", lambda *_a, **_k: [])

        ctx = _ctx()
        state = await ex.step_init(
            ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1")
        )

        pause_mock.assert_not_called()
        assert state == TaskState.LLM_CALL
