"""Tests for executor error classification + partial-work salvage (FRE-398).

Verifies:
- execute_task_safe uses the classified reason/next_step (not the generic string)
  when ctx.error is set and no partial reply was gathered.
- execute_task_safe preserves ctx.final_reply (partial work salvaged by
  step_llm_call) when ctx.error + ctx.final_reply are both set.
- _fallback_reply_from_tool_results accepts a custom lead line.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.error_classification import ClassifiedError
from personal_agent.governance.models import Mode
from personal_agent.llm_client.types import LLMServerError, LLMTimeout
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.sub_agent_types import SubAgentResult
from personal_agent.orchestrator.types import ExecutionContext, TaskState


def _make_sub_agent_result(
    task_name: str = "task_0", success: bool = True, summary: str = "Result summary"
) -> SubAgentResult:
    return SubAgentResult(
        task_id=uuid4(),
        spec_task=task_name,
        summary=summary,
        full_output=summary,
        tools_used=[],
        token_count=50,
        duration_ms=2000,
        success=success,
        error=None if success else "Timeout",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(**overrides: object) -> ExecutionContext:
    """Build a minimal ExecutionContext for testing."""
    defaults: dict[str, object] = {
        "session_id": "sess-test-001",
        "trace_id": "trace-test-001",
        "user_message": "summarize these files",
        "mode": Mode.NORMAL,
        "channel": Channel.CHAT,
    }
    defaults.update(overrides)
    return ExecutionContext(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _fallback_reply_from_tool_results
# ---------------------------------------------------------------------------


class TestFallbackReplyFromToolResults:
    def test_default_lead_with_no_results(self) -> None:
        ctx = _make_ctx()
        reply = ex._fallback_reply_from_tool_results(ctx)
        assert reply  # non-empty

    def test_custom_lead_appears_in_output(self) -> None:
        ctx = _make_ctx()
        ctx.tool_results.append({"tool_name": "read_file", "success": True})  # type: ignore[attr-defined]
        reply = ex._fallback_reply_from_tool_results(
            ctx, lead="Model failed, here's what I gathered:"
        )
        assert "Model failed" in reply
        # default lead must NOT appear
        assert "tool-use limit" not in reply

    def test_tool_results_listed(self) -> None:
        ctx = _make_ctx()
        ctx.tool_results.append({"tool_name": "search", "success": True})  # type: ignore[attr-defined]
        ctx.tool_results.append({"tool_name": "read_file", "success": False, "error": "not found"})  # type: ignore[attr-defined]
        reply = ex._fallback_reply_from_tool_results(ctx)
        assert "search" in reply
        assert "read_file" in reply


# ---------------------------------------------------------------------------
# _fallback_reply_from_tool_results / sub-agent results (FRE-1397)
#
# ctx.sub_agent_results lives outside ctx.tool_results (expansion is a
# separate path from the primary's own tool loop) — without this, a
# turn-deadline stop landing right after an expansion dispatch that
# consumed the whole remaining budget reported "no results gathered"
# despite real sub-agent output sitting in ctx.
# ---------------------------------------------------------------------------


class TestFallbackReplyFromSubAgentResults:
    def test_sub_agent_results_rendered_when_tool_results_empty(self) -> None:
        ctx = _make_ctx()
        ctx.sub_agent_results = [
            _make_sub_agent_result("evaluate_redis", summary="Redis is fast."),
            _make_sub_agent_result("evaluate_memcached", success=False, summary=""),
        ]
        reply = ex._fallback_reply_from_tool_results(ctx)
        assert "evaluate_redis" in reply
        assert "Redis is fast." in reply
        assert "evaluate_memcached" in reply
        assert "couldn't produce a final answer" not in reply

    def test_skipped_tasks_noted_alongside_results(self) -> None:
        ctx = _make_ctx()
        ctx.sub_agent_results = [_make_sub_agent_result("evaluate_redis")]
        ctx.expansion_skipped_tasks = ["evaluate_memcached"]
        reply = ex._fallback_reply_from_tool_results(ctx)
        assert "evaluate_memcached" in reply
        assert "not run" in reply.lower()

    def test_all_skipped_no_results_still_reports_skips(self) -> None:
        """AC-3 — an all-skipped plan is not silently discarded."""
        ctx = _make_ctx()
        ctx.expansion_skipped_tasks = ["evaluate_redis", "evaluate_memcached"]
        reply = ex._fallback_reply_from_tool_results(ctx)
        assert "evaluate_redis" in reply
        assert "evaluate_memcached" in reply
        assert "couldn't produce a final answer" not in reply

    def test_tool_results_take_priority_when_both_present(self) -> None:
        """The primary's own tool loop is the more specific/recent context."""
        ctx = _make_ctx()
        ctx.tool_results.append({"tool_name": "search", "success": True})  # type: ignore[attr-defined]
        ctx.sub_agent_results = [_make_sub_agent_result("evaluate_redis")]
        reply = ex._fallback_reply_from_tool_results(ctx)
        assert "search" in reply
        assert "evaluate_redis" not in reply


# ---------------------------------------------------------------------------
# _select_no_tool_final_reply (FRE-734 Defect 2)
# ---------------------------------------------------------------------------


class TestSelectNoToolFinalReply:
    """A thinking-only answer (empty content, substantive reasoning) is surfaced.

    FRE-734 Defect 2 / ADR-0101: Qwen3.6 can emit the entire vision answer in the
    reasoning/thinking channel with empty content, which previously collapsed to a
    generic 'Task completed'. The reply now falls back to the reasoning trace —
    but only when content is empty, so it is the answer, not scratchpad shadowing
    a real answer.
    """

    def test_content_wins_over_reasoning(self) -> None:
        ctx = _make_ctx()
        reply = ex._select_no_tool_final_reply(ctx, "the real answer", "some thinking")
        assert reply == "the real answer"

    def test_reasoning_surfaced_when_content_empty(self) -> None:
        ctx = _make_ctx()
        reply = ex._select_no_tool_final_reply(ctx, "", "The image shows a red bicycle.")
        assert reply == "The image shows a red bicycle."

    def test_reasoning_stripped(self) -> None:
        ctx = _make_ctx()
        reply = ex._select_no_tool_final_reply(ctx, "", "  padded answer  \n")
        assert reply == "padded answer"

    def test_empty_content_and_reasoning_falls_back(self) -> None:
        """No content, no reasoning, no tools → the generic no-answer fallback (unchanged)."""
        ctx = _make_ctx()
        reply = ex._select_no_tool_final_reply(ctx, "", None)
        assert reply == ex._fallback_reply_from_tool_results(ctx)
        assert "couldn't produce a final answer" in reply

    def test_whitespace_reasoning_does_not_shadow_tool_results(self) -> None:
        """Whitespace-only reasoning is not substantive → tool-results fallback still used."""
        ctx = _make_ctx()
        ctx.tool_results.append({"tool_name": "search", "success": True})  # type: ignore[attr-defined]
        reply = ex._select_no_tool_final_reply(ctx, "", "   ")
        assert "search" in reply


# ---------------------------------------------------------------------------
# execute_task_safe — classified reply on error (AC1)
# ---------------------------------------------------------------------------


class TestExecuteTaskSafeClassifiedReply:
    """When ctx.error is set and no partial reply, the result uses the classified message."""

    @pytest.mark.asyncio
    async def test_llm_server_error_produces_classified_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _make_ctx()
        error = LLMServerError("524 origin timeout")

        async def fake_execute_task(ctx_in: ExecutionContext, _sm: object) -> ExecutionContext:
            ctx_in.error = error
            ctx_in.state = TaskState.FAILED
            return ctx_in

        monkeypatch.setattr(ex, "execute_task", fake_execute_task)
        monkeypatch.setattr(ex, "_emit_classified_error", _noop_emit)

        result = await ex.execute_task_safe(ctx, session_manager=None)  # type: ignore[arg-type]

        reply = result["reply"]
        assert reply != "An error occurred while processing your request. Please try again."
        assert reply != "An internal error occurred. Please try again."
        assert reply  # non-empty
        # classified reply must mention retry or cloud
        assert "retry" in reply.lower() or "cloud" in reply.lower() or "error" in reply.lower()

    @pytest.mark.asyncio
    async def test_llm_timeout_produces_classified_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _make_ctx()
        error = LLMTimeout("request timed out after 251s")

        async def fake_execute_task(ctx_in: ExecutionContext, _sm: object) -> ExecutionContext:
            ctx_in.error = error
            ctx_in.state = TaskState.FAILED
            return ctx_in

        monkeypatch.setattr(ex, "execute_task", fake_execute_task)
        monkeypatch.setattr(ex, "_emit_classified_error", _noop_emit)

        result = await ex.execute_task_safe(ctx, session_manager=None)  # type: ignore[arg-type]

        reply = result["reply"]
        assert reply != "An error occurred while processing your request. Please try again."

    @pytest.mark.asyncio
    async def test_error_step_has_error_category(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _make_ctx()

        async def fake_execute_task(ctx_in: ExecutionContext, _sm: object) -> ExecutionContext:
            ctx_in.error = LLMServerError("500")
            ctx_in.state = TaskState.FAILED
            return ctx_in

        monkeypatch.setattr(ex, "execute_task", fake_execute_task)
        monkeypatch.setattr(ex, "_emit_classified_error", _noop_emit)

        result = await ex.execute_task_safe(ctx, session_manager=None)  # type: ignore[arg-type]

        error_steps = [s for s in result["steps"] if s.get("type") == "error"]
        assert error_steps, "Expected at least one error step"
        meta = error_steps[-1].get("metadata", {})
        assert "error_category" in meta
        assert meta["error_category"] == "model_server"


# ---------------------------------------------------------------------------
# execute_task_safe — partial work preserved (AC2)
# ---------------------------------------------------------------------------


class TestExecuteTaskSafePartialWorkPreserved:
    """When ctx.error + ctx.final_reply are both set, the partial reply is not discarded."""

    @pytest.mark.asyncio
    async def test_partial_reply_preserved_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _make_ctx()
        partial = (
            "Here is what I found:\n- read_file: success\n\n---\n_The model timed out. Retry._"
        )

        async def fake_execute_task(ctx_in: ExecutionContext, _sm: object) -> ExecutionContext:
            ctx_in.error = LLMServerError("524")
            ctx_in.final_reply = partial
            ctx_in.state = TaskState.FAILED
            return ctx_in

        monkeypatch.setattr(ex, "execute_task", fake_execute_task)
        monkeypatch.setattr(ex, "_emit_classified_error", _noop_emit)

        result = await ex.execute_task_safe(ctx, session_manager=None)  # type: ignore[arg-type]

        # Must NOT overwrite the partial reply with the generic or classified-only string
        assert result["reply"] == partial

    @pytest.mark.asyncio
    async def test_classified_error_stored_on_ctx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _make_ctx()

        async def fake_execute_task(ctx_in: ExecutionContext, _sm: object) -> ExecutionContext:
            ctx_in.error = LLMTimeout("251s")
            # step_llm_call would have pre-classified and saved this
            ctx_in.classified_error = ClassifiedError(
                category="timeout",
                reason="The local model timed out — the request was large.",
                next_step="Retry, switch to Cloud, or shorten it.",
                actions=("retry", "switch_to_cloud", "stop"),
                partial=False,
            )
            ctx_in.state = TaskState.FAILED
            return ctx_in

        monkeypatch.setattr(ex, "execute_task", fake_execute_task)
        monkeypatch.setattr(ex, "_emit_classified_error", _noop_emit)

        result = await ex.execute_task_safe(ctx, session_manager=None)  # type: ignore[arg-type]

        # Pre-classified reason must be used verbatim in the reply
        assert "timed out" in result["reply"].lower()

    @pytest.mark.asyncio
    async def test_emit_called_with_session_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _make_ctx()
        emitted: list[dict[str, object]] = []

        async def capturing_emit(ctx_arg: ExecutionContext, classified: ClassifiedError) -> None:
            emitted.append({"session_id": ctx_arg.session_id, "category": classified.category})

        async def fake_execute_task(ctx_in: ExecutionContext, _sm: object) -> ExecutionContext:
            ctx_in.error = LLMServerError("500")
            ctx_in.state = TaskState.FAILED
            return ctx_in

        monkeypatch.setattr(ex, "execute_task", fake_execute_task)
        monkeypatch.setattr(ex, "_emit_classified_error", capturing_emit)

        await ex.execute_task_safe(ctx, session_manager=None)  # type: ignore[arg-type]

        assert len(emitted) == 1
        assert emitted[0]["session_id"] == "sess-test-001"
        assert emitted[0]["category"] == "model_server"


# ---------------------------------------------------------------------------
# _salvage_partial_reply (FRE-973) — shared helper extracted from step_llm_call
# ---------------------------------------------------------------------------


class TestSalvagePartialReplyHelper:
    def _classified(self, **overrides: object) -> ClassifiedError:
        defaults: dict[str, object] = {
            "category": "model_server",
            "reason": "The model server returned an error.",
            "next_step": "Retry or shorten the request.",
            "actions": ("retry", "stop"),
            "partial": False,
        }
        defaults.update(overrides)
        return ClassifiedError(**defaults)  # type: ignore[arg-type]

    def test_builds_reply_and_marks_partial_when_tool_results_present(self) -> None:
        ctx = _make_ctx()
        ctx.tool_results.append({"tool_name": "query_es", "success": True})  # type: ignore[attr-defined]
        classified = self._classified()

        result = ex._salvage_partial_reply(ctx, classified, lead="Here's what I gathered:")

        assert ctx.final_reply is not None
        assert "query_es" in ctx.final_reply
        assert "Here's what I gathered" in ctx.final_reply
        assert result.partial is True

    def test_no_op_when_tool_results_empty(self) -> None:
        """No results to salvage — classified stays partial=False, no reply set."""
        ctx = _make_ctx()
        classified = self._classified()

        result = ex._salvage_partial_reply(ctx, classified, lead="Here's what I gathered:")

        assert ctx.final_reply is None

    def test_builds_reply_from_sub_agent_results_alone(self) -> None:
        """FRE-1397 — sub-agent work salvages a failure just like tool_results does."""
        ctx = _make_ctx()
        ctx.sub_agent_results = [_make_sub_agent_result("evaluate_redis")]
        classified = self._classified()

        result = ex._salvage_partial_reply(ctx, classified, lead="Here's what I gathered:")

        assert ctx.final_reply is not None
        assert "evaluate_redis" in ctx.final_reply
        assert result.partial is True

    def test_idempotent_second_call_does_not_overwrite(self) -> None:
        ctx = _make_ctx()
        ctx.tool_results.append({"tool_name": "query_es", "success": True})  # type: ignore[attr-defined]
        classified = self._classified()

        first = ex._salvage_partial_reply(ctx, classified, lead="First lead:")
        first_reply = ctx.final_reply
        second = ex._salvage_partial_reply(
            ctx, self._classified(), lead="Second lead — should not appear:"
        )

        assert ctx.final_reply == first_reply
        assert "Second lead" not in (ctx.final_reply or "")


# ---------------------------------------------------------------------------
# _stop_turn_for_deadline (FRE-973 / FRE-1397 AC-2)
#
# This is the exact path a turn takes when an expansion dispatch consumes
# the whole remaining budget: step_llm_call's own pre-call deadline check
# fires before the primary ever attempts a synthesis call. AC-2 requires the
# turn to still "reach synthesis with whatever completed" — this is what
# makes that true when synthesis never actually gets to run.
# ---------------------------------------------------------------------------


class TestStopTurnForDeadline:
    def test_reports_sub_agent_work_instead_of_generic_message(self) -> None:
        ctx = _make_ctx()
        ctx.sub_agent_results = [_make_sub_agent_result("evaluate_redis", summary="Redis is fast.")]

        ex._stop_turn_for_deadline(ctx)

        assert ctx.final_reply is not None
        assert "evaluate_redis" in ctx.final_reply
        assert "before gathering any results" not in ctx.final_reply
        assert ctx.turn_stopped_early is True

    def test_all_skipped_still_reports_the_gap(self) -> None:
        ctx = _make_ctx()
        ctx.expansion_skipped_tasks = ["evaluate_redis", "evaluate_memcached"]

        ex._stop_turn_for_deadline(ctx)

        assert ctx.final_reply is not None
        assert "evaluate_redis" in ctx.final_reply
        assert "before gathering any results" not in ctx.final_reply

    def test_generic_message_unchanged_when_nothing_gathered(self) -> None:
        ctx = _make_ctx()

        ex._stop_turn_for_deadline(ctx)

        assert ctx.final_reply is not None
        assert "before gathering any results" in ctx.final_reply


# ---------------------------------------------------------------------------
# execute_task's outer except now salvages too (FRE-973) — previously only
# step_llm_call's own local except did, so an exception raised anywhere else
# in the state-machine loop silently dropped ctx.tool_results.
# ---------------------------------------------------------------------------


class TestExecuteTaskOuterExceptSalvage:
    @pytest.mark.asyncio
    async def test_error_outside_step_llm_call_still_salvages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import contextlib

        ctx = _make_ctx()
        ctx.tool_results.append({"tool_name": "query_es", "success": True})  # type: ignore[attr-defined]
        ctx.state = TaskState.TOOL_EXECUTION

        @contextlib.asynccontextmanager
        async def fake_observe_topology(_ctx: ExecutionContext):
            yield

        async def raising_step_tool_execution(
            ctx_in: ExecutionContext, _sm: object, _trace_ctx: object
        ) -> TaskState:
            raise LLMServerError("524 origin timeout")

        monkeypatch.setattr(ex, "observe_topology", fake_observe_topology)
        monkeypatch.setattr(ex, "step_tool_execution", raising_step_tool_execution)

        result_ctx = await ex.execute_task(ctx, session_manager=None)  # type: ignore[arg-type]

        assert result_ctx.final_reply is not None
        assert "query_es" in result_ctx.final_reply
        assert result_ctx.classified_error is not None
        assert result_ctx.classified_error.partial is True
        assert result_ctx.classified_error.category == "model_server"
        assert result_ctx.state == TaskState.FAILED


# ---------------------------------------------------------------------------
# execute_task_safe's outer except — last-resort salvage net (FRE-973)
# ---------------------------------------------------------------------------


class TestExecuteTaskSafeLastResortSalvage:
    @pytest.mark.asyncio
    async def test_returned_reply_uses_salvaged_text_not_hardcoded_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """execute_task itself raising must not discard prior steps or a
        reply salvaged before the raise (previously this except hardcoded its
        return, ignoring ctx.final_reply and replacing ctx.steps outright).
        """
        ctx = _make_ctx()
        prior_step = {"type": "tool_call", "description": "query_es", "metadata": {}}
        ctx.steps.append(prior_step)  # type: ignore[attr-defined]
        salvaged_reply = "Here is what I found before things went wrong."

        async def fake_execute_task(ctx_in: ExecutionContext, _sm: object) -> ExecutionContext:
            ctx_in.final_reply = salvaged_reply
            raise LLMServerError("524 origin timeout")

        monkeypatch.setattr(ex, "execute_task", fake_execute_task)
        monkeypatch.setattr(ex, "_emit_classified_error", _noop_emit)

        result = await ex.execute_task_safe(ctx, session_manager=None)  # type: ignore[arg-type]

        assert result["reply"] == salvaged_reply
        assert prior_step in result["steps"]
        error_steps = [s for s in result["steps"] if s.get("type") == "error"]
        assert error_steps, "Expected the new error step to be appended, not replace prior steps"

    @pytest.mark.asyncio
    async def test_falls_back_to_classified_message_when_nothing_salvaged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _make_ctx()

        async def fake_execute_task(ctx_in: ExecutionContext, _sm: object) -> ExecutionContext:
            raise LLMServerError("524 origin timeout")

        monkeypatch.setattr(ex, "execute_task", fake_execute_task)
        monkeypatch.setattr(ex, "_emit_classified_error", _noop_emit)

        result = await ex.execute_task_safe(ctx, session_manager=None)  # type: ignore[arg-type]

        assert result["reply"] != ""
        assert "retry" in result["reply"].lower() or "error" in result["reply"].lower()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _noop_emit(_ctx: ExecutionContext, _classified: ClassifiedError) -> None:
    pass
