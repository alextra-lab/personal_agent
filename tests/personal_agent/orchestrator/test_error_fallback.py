"""Tests for executor error classification + partial-work salvage (FRE-398).

Verifies:
- execute_task_safe uses the classified reason/next_step (not the generic string)
  when ctx.error is set and no partial reply was gathered.
- execute_task_safe preserves ctx.final_reply (partial work salvaged by
  step_llm_call) when ctx.error + ctx.final_reply are both set.
- _fallback_reply_from_tool_results accepts a custom lead line.
"""

from __future__ import annotations

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.error_classification import ClassifiedError
from personal_agent.governance.models import Mode
from personal_agent.llm_client.types import LLMServerError, LLMTimeout
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.types import ExecutionContext, TaskState

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
# Helpers
# ---------------------------------------------------------------------------


async def _noop_emit(_ctx: ExecutionContext, _classified: ClassifiedError) -> None:
    pass
