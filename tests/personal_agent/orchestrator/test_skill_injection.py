"""Tests for skill block injection into the executor system prompt.

These tests verify that ``step_llm_call`` in executor.py correctly wires
the skill block (from ``get_skill_block``) into the system prompt passed to
the LLM client.

Two complementary approaches are used:
1. Source-level structural test — asserts the injection pattern is present
   in executor.py (fast, zero dependencies, always correct).
2. Functional mock test — patches ``get_skill_block`` and ``get_llm_client``
   and drives ``step_llm_call``, then inspects the system_prompt argument.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Structural tests (no executor import side-effects)
# ---------------------------------------------------------------------------

_EXECUTOR_SRC = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "personal_agent"
    / "orchestrator"
    / "executor.py"
)


class TestSkillInjectionStructure:
    """Structural tests that verify the injection code exists in executor.py."""

    def test_get_skill_block_import_present(self) -> None:
        """executor.py must import get_skill_block inside step_llm_call."""
        source = _EXECUTOR_SRC.read_text(encoding="utf-8")
        assert "from personal_agent.orchestrator.skills import get_skill_block" in source

    def test_skill_block_used_to_build_system_prompt(self) -> None:
        """executor.py must assign get_skill_block() and splice it into system_prompt."""
        source = _EXECUTOR_SRC.read_text(encoding="utf-8")
        assert "skill_block = get_skill_block()" in source
        assert "if skill_block:" in source

    def test_skill_block_injected_when_flag_enabled(self) -> None:
        """executor.py must contain the branch that appends skill_block to system_prompt."""
        source = _EXECUTOR_SRC.read_text(encoding="utf-8")
        # When a system_prompt already exists, it is extended with the skill block
        assert "system_prompt = f\"{system_prompt}\\n\\n{skill_block}\"" in source

    def test_skill_block_becomes_system_prompt_when_no_prior_prompt(self) -> None:
        """executor.py must handle the case where system_prompt is None by setting it to skill_block."""
        source = _EXECUTOR_SRC.read_text(encoding="utf-8")
        assert "system_prompt = skill_block" in source

    def test_injection_before_llm_call(self) -> None:
        """Skill block injection must appear before the llm_client.respond() call in source order."""
        source = _EXECUTOR_SRC.read_text(encoding="utf-8")
        inject_pos = source.find("skill_block = get_skill_block()")
        respond_pos = source.find("response = await llm_client.respond(")
        assert inject_pos != -1, "Injection line not found"
        assert respond_pos != -1, "llm_client.respond call not found"
        assert inject_pos < respond_pos, (
            "Skill block injection must happen before the LLM respond() call"
        )


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
