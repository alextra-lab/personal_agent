"""Tests for skill block injection into the executor system prompt.

Functional mock tests: patches ``get_skill_block`` and ``get_llm_client``
and drives ``step_llm_call``, then inspects the system_prompt argument.
Source-level structural tests were removed (FRE-320) — they coupled to
internal variable names that change on refactor; the functional tests below
cover the same behaviour more robustly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Functional mock tests — drive step_llm_call with mocked LLM
# ---------------------------------------------------------------------------

_SENTINEL = "## SKILL TEST CONTENT"


def _make_minimal_ctx() -> object:
    """Build a minimal ExecutionContext sufficient to reach the skill injection point."""
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext

    ctx = ExecutionContext(
        session_id="test-session",
        trace_id="test-trace",
        user_message="hello",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=[{"role": "user", "content": "hello"}],
    )
    return ctx


def _make_minimal_response() -> dict[str, object]:
    """Create a minimal LLM response dict that executor can parse."""
    return {
        "content": "I understand.",
        "tool_calls": [],
        "response_id": None,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _make_mock_llm_client(response: dict[str, object]) -> MagicMock:
    """Build a mock LLM client that records its respond() call."""
    mock_client = MagicMock()
    mock_client.respond = AsyncMock(return_value=response)
    # model_configs is accessed to determine ToolCallingStrategy
    mock_client.model_configs = {}
    return mock_client


class TestSkillBlockFunctionalInjection:
    """Functional tests: drive step_llm_call and inspect the system_prompt passed to LLM."""

    @pytest.mark.asyncio
    async def test_skill_block_injected_when_flag_enabled(self) -> None:
        """When get_skill_block() returns content, it must appear in the system_prompt."""
        from personal_agent.telemetry.trace import TraceContext

        # get_skill_block is patched to return a sentinel — settings flag is irrelevant here.
        # Flag gating is separately tested in test_skills.py::TestFlagGating.

        ctx = _make_minimal_ctx()
        trace_ctx = TraceContext.new_trace()
        mock_llm = _make_mock_llm_client(_make_minimal_response())
        mock_session = MagicMock()
        mock_session.add_message = AsyncMock()
        mock_session.get_messages = AsyncMock(return_value=[])

        with (
            patch(
                "personal_agent.orchestrator.skills.get_skill_block",
                return_value=_SENTINEL,
            ),
            patch(
                "personal_agent.llm_client.factory.get_llm_client",
                return_value=mock_llm,
            ),
            patch(
                "personal_agent.orchestrator.executor.get_default_registry",
                return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
            ),
        ):
            from personal_agent.orchestrator.executor import step_llm_call

            await step_llm_call(ctx, mock_session, trace_ctx)  # type: ignore[arg-type]

        # Inspect the system_prompt keyword argument passed to the LLM client
        assert mock_llm.respond.called, "LLM client was not called"
        call_kwargs = mock_llm.respond.call_args.kwargs
        system_prompt_passed = call_kwargs.get("system_prompt", "")
        assert _SENTINEL in (system_prompt_passed or ""), (
            f"Sentinel not found in system_prompt: {system_prompt_passed!r}"
        )

    @pytest.mark.asyncio
    async def test_skill_block_not_injected_when_flag_disabled(self) -> None:
        """When get_skill_block() returns '', the sentinel must NOT appear in system_prompt."""
        from personal_agent.telemetry.trace import TraceContext

        # get_skill_block is patched to return a sentinel — settings flag is irrelevant here.
        # Flag gating is separately tested in test_skills.py::TestFlagGating.

        ctx = _make_minimal_ctx()
        trace_ctx = TraceContext.new_trace()
        mock_llm = _make_mock_llm_client(_make_minimal_response())
        mock_session = MagicMock()
        mock_session.add_message = AsyncMock()
        mock_session.get_messages = AsyncMock(return_value=[])

        with (
            patch(
                "personal_agent.orchestrator.skills.get_skill_block",
                return_value="",
            ),
            patch(
                "personal_agent.llm_client.factory.get_llm_client",
                return_value=mock_llm,
            ),
            patch(
                "personal_agent.orchestrator.executor.get_default_registry",
                return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
            ),
        ):
            from personal_agent.orchestrator.executor import step_llm_call

            await step_llm_call(ctx, mock_session, trace_ctx)  # type: ignore[arg-type]

        assert mock_llm.respond.called, "LLM client was not called"
        call_kwargs = mock_llm.respond.call_args.kwargs
        system_prompt_passed = call_kwargs.get("system_prompt", "")
        assert _SENTINEL not in (system_prompt_passed or ""), (
            f"Sentinel unexpectedly found in system_prompt: {system_prompt_passed!r}"
        )
