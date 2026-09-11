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
from personal_agent.orchestrator.executor import step_synthesis
from personal_agent.orchestrator.expansion_controller import ExpansionResult
from personal_agent.orchestrator.session import SessionManager
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


class TestFanoutPauseTasks:
    """Pure pause predicate (ADR-0149 D4, amended 2026-09-11) — no pause/decision
    machinery involved. Guards a landing that FAILED, not merely a task that
    is incomplete."""

    def test_complete_fanout_is_empty(self) -> None:
        results = [_sub_result("a"), _sub_result("b")]
        assert ex._fanout_pause_tasks(results, []) == []

    def test_capped_synthesized_report_is_not_a_pause_task(self) -> None:
        """Fixture A — stopped at the cap with a good synthesized report: it
        landed, even though its task is incomplete. Must NOT pause."""
        results = [_sub_result("a", success=False, stop_reason="cap", report_kind="synthesized")]
        assert ex._fanout_pause_tasks(results, []) == []

    def test_ledger_is_a_pause_task(self) -> None:
        """Fixture B — completed but the content is a bare ledger, no report."""
        results = [_sub_result("a", success=False, stop_reason="completed", report_kind="ledger")]
        assert ex._fanout_pause_tasks(results, []) == [("a", "completed")]

    def test_narration_is_a_pause_task(self) -> None:
        """Fixture C — the report-writing call was cut mid-write."""
        results = [_sub_result("a", success=False, stop_reason="timeout", report_kind="narration")]
        assert ex._fanout_pause_tasks(results, []) == [("a", "timeout")]

    def test_skipped_task_is_a_pause_task(self) -> None:
        assert ex._fanout_pause_tasks([], ["never_dispatched"]) == [
            ("never_dispatched", "not_dispatched")
        ]


class TestFanoutTrailerTasks:
    """Pure trailer predicate — broader than the pause's: any incomplete task,
    landing failed or not."""

    def test_complete_fanout_is_empty(self) -> None:
        results = [_sub_result("a"), _sub_result("b")]
        assert ex._fanout_trailer_tasks(results, []) == []

    def test_capped_synthesized_report_is_a_trailer_task(self) -> None:
        """Fixture A — landed, but the task itself is still incomplete."""
        results = [_sub_result("a", success=False, stop_reason="cap", report_kind="synthesized")]
        assert ex._fanout_trailer_tasks(results, []) == [("a", "cap")]

    def test_ledger_is_a_trailer_task(self) -> None:
        results = [_sub_result("a", success=False, stop_reason="completed", report_kind="ledger")]
        assert ex._fanout_trailer_tasks(results, []) == [("a", "completed")]

    def test_skipped_task_is_a_trailer_task(self) -> None:
        assert ex._fanout_trailer_tasks([], ["never_dispatched"]) == [
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


class TestStepInitPausesOnFailedLanding:
    """AC-7 (amended 2026-09-11) — a fan-out with a failed landing (a ledger
    or a narration, or a skipped task) cannot reach synthesis unpaused; a
    fan-out without one is never paused, even when a task is incomplete."""

    @pytest.mark.asyncio
    async def test_fixture_a_cap_synthesized_does_not_pause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Landed — a good synthesized report — even though the task is
        incomplete. Must not pause; the trailer still applies."""
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[
                _sub_result("a", success=False, stop_reason="cap", report_kind="synthesized")
            ],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        pause_mock = AsyncMock(
            return_value=ConstraintDecision("answer_from_partial", "user_choice")
        )
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", pause_mock)

        ctx = _ctx()
        state = await ex.step_init(
            ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1")
        )

        pause_mock.assert_not_called()
        assert state == TaskState.LLM_CALL
        assert ctx.fanout_trailer is not None
        assert "a: cap" in ctx.fanout_trailer

    @pytest.mark.asyncio
    async def test_fixture_b_ledger_pauses(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        assert pause_mock.await_args.kwargs["constraint"] == "sub_agent_fanout_incomplete"

    @pytest.mark.asyncio
    async def test_fixture_c_narration_pauses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[
                _sub_result("a", success=False, stop_reason="timeout", report_kind="narration")
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
    async def test_all_successful_no_pause_no_trailer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
    """AC-2 — stop_and_show makes no model call and shows every report.

    Requires a pause-eligible fixture (ledger/narration) — stop_and_show is
    never offered on fixture A (cap + synthesized), which does not pause.
    """

    @pytest.mark.asyncio
    async def test_stop_and_show_returns_synthesis_with_full_reports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[
                _sub_result(
                    "a",
                    success=False,
                    stop_reason="completed",
                    report_kind="ledger",
                    full_output="MARKER_REPORT_A",
                )
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
        assert "stop=completed" in ctx.final_reply
        # No synthesis message was queued for a further LLM call.
        assert not any(
            m.get("role") == "user" and "Synthesize the results" in str(m.get("content", ""))
            for m in ctx.messages
        )
        # FRE-1375 convention: a deterministic, ungenerated reply is not a claim
        # to verify — step_synthesis must skip grounding verification for it
        # exactly as it does for a deadline/lifetime-cap/user-cancel stop.
        assert ctx.turn_stopped_early is True
        # stop_and_show makes no synthesis call — nothing for a trailer to
        # attach to.
        assert ctx.fanout_trailer is None

    @pytest.mark.asyncio
    async def test_stop_and_show_reply_survives_grounding_enforce_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The composed reply is not re-verified, retried, or overwritten.

        Without ``turn_stopped_early``, ``step_synthesis`` under
        ``grounding_verification_mode == "enforce"`` would run an entailment
        model call over this deterministic text and could replace it with a
        retry directive or a generic "no source" statement — silently losing
        the worker reports this mechanism exists to surface verbatim.
        """
        from unittest.mock import patch

        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[
                _sub_result(
                    "a",
                    success=False,
                    stop_reason="completed",
                    report_kind="ledger",
                    full_output="MARKER_REPORT_A",
                )
            ],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        monkeypatch.setattr(
            ex,
            "_maybe_pause_for_constraint",
            AsyncMock(return_value=ConstraintDecision("stop_and_show", "user_choice")),
        )

        from personal_agent.grounding.source_registry import SourceRegistry

        ctx = _ctx()
        trace_ctx = TraceContext(trace_id="t1", session_id="s1")
        state = await ex.step_init(ctx, _session_manager(), trace_ctx)
        assert state == TaskState.SYNTHESIS
        composed_reply = ctx.final_reply
        # A real turn has a source registry by the time step_synthesis runs
        # (built in execute_task before step_init) — without it, _verify_grounding
        # short-circuits to "unavailable" regardless of turn_stopped_early, which
        # would make this test pass vacuously.
        ctx.source_registry = SourceRegistry(turn_id=ctx.trace_id)

        entailment_extract = AsyncMock()
        session_manager = _session_manager()
        session_manager.update_session = MagicMock()
        with (
            patch("personal_agent.orchestrator.executor.settings") as cfg,
            patch(
                "personal_agent.grounding.extractor.ModelSpanExtractor",
                return_value=MagicMock(extract=entailment_extract),
            ),
        ):
            cfg.grounding_verification_mode = "enforce"
            cfg.grounding_max_generation_attempts = 2
            cfg.environment = "test"
            cfg.grounding_entailment_sample_rate = 0.0
            cfg.grounding_entailment_max_inline_checks = 8
            cfg.grounding_entailment_latency_budget_ms = 4000
            cfg.grounding_entailment_max_excerpt_chars = 6000
            final_state = await step_synthesis(ctx, session_manager, trace_ctx)

        assert final_state == TaskState.COMPLETED
        assert ctx.final_reply == composed_reply
        entailment_extract.assert_not_called()


class TestAnswerFromPartialCarriesTrailer:
    """AC-3 — the trailer is present and names the tasks, whether from an
    interactive answer_from_partial decision after a pause (a ledger fixture)
    or with no pause at all (fixture A)."""

    @pytest.mark.asyncio
    async def test_trailer_set_on_answer_from_partial_after_a_pause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[
                _sub_result("a", success=False, stop_reason="completed", report_kind="ledger")
            ],
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
        assert "a: completed" in ctx.fanout_trailer

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
    """AC-4 (amended 2026-09-11) — an eval fan-out never waits on the pause
    timeout, and its default is answer_from_partial with the trailer — not
    stop_and_show, which now needs an explicitly stored preference. All
    require a pause-eligible (ledger) fixture; fixture A never reaches the
    eval_mode branch at all since it never pauses."""

    @staticmethod
    def _ledger_exp_result() -> ExpansionResult:
        return ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[
                _sub_result("a", success=False, stop_reason="completed", report_kind="ledger")
            ],
            synthesis_context="SYN",
        )

    @pytest.mark.asyncio
    async def test_no_preference_resolves_answer_from_partial_with_trailer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_expansion(monkeypatch, self._ledger_exp_result())
        pause_mock = AsyncMock()
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", pause_mock)
        monkeypatch.setattr(ex, "_load_constraint_preference", AsyncMock(return_value=None))

        ctx = _ctx(eval_mode=True)
        state = await ex.step_init(
            ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1")
        )

        pause_mock.assert_not_called()
        assert state == TaskState.LLM_CALL
        assert ctx.fanout_trailer is not None

    @pytest.mark.asyncio
    async def test_always_pause_preference_resolves_answer_from_partial_with_trailer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_expansion(monkeypatch, self._ledger_exp_result())
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
        assert state == TaskState.LLM_CALL
        assert ctx.fanout_trailer is not None

    @pytest.mark.asyncio
    async def test_stored_answer_from_partial_preference_resolves_the_same_way(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_expansion(monkeypatch, self._ledger_exp_result())
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

    @pytest.mark.asyncio
    async def test_stored_stop_and_show_preference_applies_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_expansion(monkeypatch, self._ledger_exp_result())
        pause_mock = AsyncMock()
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", pause_mock)
        monkeypatch.setattr(
            ex, "_load_constraint_preference", AsyncMock(return_value="stop_and_show")
        )

        ctx = _ctx(eval_mode=True)
        state = await ex.step_init(
            ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1")
        )

        pause_mock.assert_not_called()
        assert state == TaskState.SYNTHESIS
        assert ctx.final_reply is not None
        assert ctx.fanout_trailer is None


class TestSeededNegativeD4Pause:
    """AC-6 — disabling the D4 pause predicate must fail AC-7: a fan-out with
    a failed landing reaches synthesis unpaused.
    """

    @pytest.mark.asyncio
    async def test_disabling_the_predicate_lets_a_failed_landing_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[
                _sub_result("a", success=False, stop_reason="completed", report_kind="ledger")
            ],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        pause_mock = AsyncMock()
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", pause_mock)
        # The disabled mechanism: the predicate always reports nothing failed.
        monkeypatch.setattr(ex, "_fanout_pause_tasks", lambda *_a, **_k: [])

        ctx = _ctx()
        state = await ex.step_init(
            ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1")
        )

        pause_mock.assert_not_called()
        assert state == TaskState.LLM_CALL


class TestStopAndShowPersistsToHistory:
    """Master bounce, Medium — stop_and_show must append an assistant message.

    Before this fix, ``ctx.final_reply`` was set but nothing was appended to
    ``ctx.messages``, so a reusable ``Orchestrator``/``SessionManager`` caller
    lost the composed report from in-memory history (the HTTP path was
    unaffected — it persists ``result["reply"]`` via ``RequestCompletedEvent``).
    """

    @pytest.mark.asyncio
    async def test_assistant_message_appended_before_synthesis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[
                _sub_result(
                    "a",
                    success=False,
                    stop_reason="completed",
                    report_kind="ledger",
                    full_output="MARKER_REPORT_A",
                )
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
        assert ctx.messages[-1]["role"] == "assistant"
        assert ctx.messages[-1]["content"] == ctx.final_reply
        assert "MARKER_REPORT_A" in ctx.messages[-1]["content"]


class TestFanoutDecisionRecordedOnTurnEvidence:
    """ADR-0149 D4 (amended 2026-09-11) — the applied option and its source
    reach the turn's own record (``ctx.steps`` → ``OrchestratorResult["steps"]``),
    not only a log line, so a study can read which policy each of its turns
    ran under.
    """

    @pytest.mark.asyncio
    async def test_interactive_pause_decision_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[
                _sub_result("a", success=False, stop_reason="completed", report_kind="ledger")
            ],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        monkeypatch.setattr(
            ex,
            "_maybe_pause_for_constraint",
            AsyncMock(return_value=ConstraintDecision("answer_from_partial", "user_choice")),
        )

        ctx = _ctx()
        await ex.step_init(ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1"))

        entries = [s for s in ctx.steps if s.get("metadata", {}).get("sub_agent_fanout_decision")]
        assert len(entries) == 1
        assert entries[0]["metadata"]["sub_agent_fanout_decision"] == "answer_from_partial"
        assert entries[0]["metadata"]["sub_agent_fanout_decision_source"] == "user_choice"

    @pytest.mark.asyncio
    async def test_eval_default_decision_recorded_with_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[
                _sub_result("a", success=False, stop_reason="completed", report_kind="ledger")
            ],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", AsyncMock())
        monkeypatch.setattr(ex, "_load_constraint_preference", AsyncMock(return_value=None))

        ctx = _ctx(eval_mode=True)
        await ex.step_init(ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1"))

        entries = [s for s in ctx.steps if s.get("metadata", {}).get("sub_agent_fanout_decision")]
        assert len(entries) == 1
        assert entries[0]["metadata"]["sub_agent_fanout_decision"] == "answer_from_partial"
        assert entries[0]["metadata"]["sub_agent_fanout_decision_source"] == "default"

    @pytest.mark.asyncio
    async def test_fixture_a_no_pause_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No decision was made (nothing paused) — nothing to record."""
        exp_result = ExpansionResult(
            plan=MagicMock(is_fallback=False),
            sub_agent_results=[
                _sub_result("a", success=False, stop_reason="cap", report_kind="synthesized")
            ],
            synthesis_context="SYN",
        )
        _patch_expansion(monkeypatch, exp_result)
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", AsyncMock())

        ctx = _ctx()
        await ex.step_init(ctx, _session_manager(), TraceContext(trace_id="t1", session_id="s1"))

        entries = [s for s in ctx.steps if s.get("metadata", {}).get("sub_agent_fanout_decision")]
        assert entries == []


class TestTrailerExitMatrix:
    """Master bounce, High — the trailer/history invariant, exit by exit.

    AC-3 claims the wire form equals persisted history. That holds
    unconditionally on two exits: normal completion (already covered by
    ``tests/test_orchestrator/test_executor.py::TestStepSynthesisFanoutTrailer``)
    and a grounding-enforced replacement of ``final_reply`` — proven here, now
    that ``step_synthesis`` syncs the assistant message FROM the finalized
    ``final_reply`` rather than concatenating onto its own (possibly stale)
    content. A deadline/lifetime-cap/cancel salvage never appends an assistant
    message at all, trailer or not — proven here to leave history untouched
    (no corruption) while the wire reply still carries the trailer. A
    synthesis-call exception never reaches ``step_synthesis`` at all (it
    returns ``TaskState.FAILED`` from ``step_llm_call``), so there is no
    trailer to lose — nothing here applies, and no test exercises what cannot
    execute.
    """

    _CLAIM = "Paris has 2.1 million residents"

    def _extraction_of(self, reply: str) -> "SpanExtraction":
        from personal_agent.grounding.spans import (
            NonExemptReason,
            Span,
            SpanExtraction,
            SpanLabel,
        )

        start = reply.index(self._CLAIM)
        return SpanExtraction(
            output=reply,
            spans=(
                Span(
                    start=start,
                    end=start + len(self._CLAIM),
                    text=self._CLAIM,
                    label=SpanLabel.CLAIM_NON_EXEMPT,
                    reason=NonExemptReason.CLASSIFIED,
                ),
            ),
        )

    def _patched_extractor(self, reply: str):
        from unittest.mock import patch

        extractor = AsyncMock()
        extractor.extract = AsyncMock(return_value=self._extraction_of(reply))
        return (
            patch(
                "personal_agent.grounding.extractor.ModelSpanExtractor",
                return_value=extractor,
            ),
            patch("personal_agent.llm_client.factory.get_llm_client", return_value=object()),
        )

    def _grounded_ctx(self, reply: str, trailer: str) -> ExecutionContext:
        from personal_agent.grounding.source_registry import SourceRegistry

        ctx = ExecutionContext(
            session_id="sess-1484-matrix",
            trace_id="trace-1484-matrix",
            user_message="research this",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )
        ctx.final_reply = reply
        ctx.fanout_trailer = trailer
        ctx.source_registry = SourceRegistry(turn_id=ctx.trace_id)
        ctx.messages = [
            {"role": "user", "content": "research this"},
            {"role": "assistant", "content": reply},
        ]
        return ctx

    async def _synthesize(self, ctx: ExecutionContext, reply: str) -> TaskState:
        session_manager = SessionManager()
        session_manager.create_session(Mode.NORMAL, Channel.CHAT, session_id=ctx.session_id)
        extractor_patch, client_patch = self._patched_extractor(reply)
        with extractor_patch, client_patch:
            return await step_synthesis(
                ctx,
                session_manager,
                TraceContext(trace_id=ctx.trace_id, session_id=ctx.session_id),
            )

    @pytest.mark.asyncio
    async def test_grounding_retry_leaves_the_carrier_set_for_the_next_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RETRY_WITH_FORCED_RETRIEVAL returns to LLM_CALL before the trailer
        block runs — the carrier must survive so the eventual completing pass
        still applies it, not be cleared or applied prematurely."""
        from unittest.mock import patch

        reply = f"{self._CLAIM}."
        trailer = "\n\n— Research note: 1 of 2 sub-tasks did not complete (b: cap)."
        ctx = self._grounded_ctx(reply, trailer)

        with patch("personal_agent.orchestrator.executor.settings") as cfg:
            cfg.grounding_verification_mode = "enforce"
            cfg.grounding_max_generation_attempts = 2
            cfg.environment = "test"
            cfg.grounding_entailment_sample_rate = 0.0
            cfg.grounding_entailment_max_inline_checks = 8
            cfg.grounding_entailment_latency_budget_ms = 4000
            cfg.grounding_entailment_max_excerpt_chars = 6000
            state = await self._synthesize(ctx, reply)

        assert state == TaskState.LLM_CALL
        assert ctx.final_reply is None
        assert ctx.fanout_trailer == trailer

    @pytest.mark.asyncio
    async def test_grounding_terminal_replacement_syncs_history_to_final_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TERMINAL_NO_SOURCE replaces final_reply — history must sync to that
        replacement plus the trailer, not the stale pre-replacement content."""
        from unittest.mock import patch

        reply = f"{self._CLAIM}."
        trailer = "\n\n— Research note: 1 of 2 sub-tasks did not complete (b: cap)."
        ctx = self._grounded_ctx(reply, trailer)
        ctx.grounding_attempts = 1  # a retry already happened — this attempt is terminal
        ctx.retrieval_attempts = ["web_search(paris population)"]

        with patch("personal_agent.orchestrator.executor.settings") as cfg:
            cfg.grounding_verification_mode = "enforce"
            cfg.grounding_max_generation_attempts = 2
            cfg.environment = "test"
            cfg.grounding_entailment_sample_rate = 0.0
            cfg.grounding_entailment_max_inline_checks = 8
            cfg.grounding_entailment_latency_budget_ms = 4000
            cfg.grounding_entailment_max_excerpt_chars = 6000
            state = await self._synthesize(ctx, reply)

        assert state == TaskState.COMPLETED
        assert ctx.final_reply is not None
        assert self._CLAIM not in ctx.final_reply
        assert ctx.final_reply.endswith(trailer)
        # History must equal the wire form exactly — not the stale claim-bearing
        # text with the trailer merely appended on top of it.
        assert ctx.messages[-1]["content"] == ctx.final_reply
        assert self._CLAIM not in ctx.messages[-1]["content"]
        assert ctx.fanout_trailer is None

    @pytest.mark.asyncio
    async def test_deadline_salvage_carries_no_assistant_message_to_sync(self) -> None:
        """A deadline/lifetime-cap/cancel salvage never appends an assistant
        message (trailer or not) — the wire reply still gets the trailer;
        persisted history is left alone rather than corrupting an unrelated
        (e.g. tool-role) last message."""
        ctx = ExecutionContext(
            session_id="sess-1484-deadline",
            trace_id="trace-1484-deadline",
            user_message="research this",
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
        )
        salvaged_reply = "This turn was stopped early — here's what was gathered so far."
        trailer = "\n\n— Research note: 1 of 2 sub-tasks did not complete (b: cap)."
        ctx.final_reply = salvaged_reply
        ctx.fanout_trailer = trailer
        ctx.turn_stopped_early = True  # set by _stop_turn_for_deadline, as it would be
        ctx.messages = [
            {"role": "user", "content": "research this"},
            {"role": "tool", "tool_call_id": "c0", "content": "partial tool output"},
        ]
        stale_last_message = dict(ctx.messages[-1])

        session_manager = SessionManager()
        session_manager.create_session(Mode.NORMAL, Channel.CHAT, session_id=ctx.session_id)
        state = await step_synthesis(
            ctx, session_manager, TraceContext(trace_id=ctx.trace_id, session_id=ctx.session_id)
        )

        assert state == TaskState.COMPLETED
        assert ctx.final_reply == f"{salvaged_reply}{trailer}"
        assert ctx.messages[-1] == stale_last_message
        assert ctx.fanout_trailer is None
