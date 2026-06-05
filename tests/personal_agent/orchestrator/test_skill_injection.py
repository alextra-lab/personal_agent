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


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals() -> object:
    """Restore the executor's lazily-cached registry globals after each test.

    These tests patch ``executor.get_default_registry`` and drive ``step_llm_call``,
    which seeds the module-level ``_tool_registry`` / ``_tool_execution_layer`` from
    the *patched* registry. Without this restore the patched (small) registry leaks
    past the patch scope and pollutes later tests in the process (e.g. the primary
    tool-flow tests that expect the real registry). Snapshot-and-restore keeps the
    leak contained regardless of collection order.
    """
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    saved_layer = _ex._tool_execution_layer
    yield
    _ex._tool_registry = saved_registry
    _ex._tool_execution_layer = saved_layer


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
    async def test_skill_block_injected_when_flag_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When get_skill_block() returns content, it must appear in the system_prompt.

        ``get_skill_block`` is only called in the ``keyword`` and ``hybrid``
        routing paths. ``model_decided`` (the production default since the
        2026-05-10 routing fix) bypasses it in favour of the model-routed
        skill index. Force ``hybrid`` here so the patched function is exercised.
        """
        from personal_agent.config import settings
        from personal_agent.telemetry.trace import TraceContext

        monkeypatch.setattr(settings, "cache_frozen_layout_enabled", False)
        monkeypatch.setattr(settings, "prefer_primitives_enabled", True)
        monkeypatch.setattr(settings, "skill_routing_mode", "hybrid")
        monkeypatch.setattr(settings, "skill_routing_model_key", "")

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
    async def test_skill_block_not_injected_when_flag_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When get_skill_block() returns '', the sentinel must NOT appear in system_prompt.

        Pinned to ``hybrid`` routing for the same reason as the companion test
        above: ``get_skill_block`` is the keyword/hybrid-path entry point.
        """
        from personal_agent.config import settings
        from personal_agent.telemetry.trace import TraceContext

        monkeypatch.setattr(settings, "skill_routing_mode", "hybrid")
        monkeypatch.setattr(settings, "skill_routing_model_key", "")

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


_INDEX_DIRECTIVE_SENTINEL = "<skill_index_directive>"
_USAGE_DIRECTIVE_SENTINEL = "<skill_usage_directives>"


def _make_nudge_patches(
    skill_injection: str,
    index_directive_return: str,
    usage_directives_return: str,
) -> tuple:
    return (
        patch("personal_agent.orchestrator.skills.get_skill_block", return_value=skill_injection),
        patch("personal_agent.orchestrator.skills.assemble_skill_index", return_value=""),
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_index_directive",
            return_value=index_directive_return,
        ),
        patch(
            "personal_agent.orchestrator.skills.assemble_skill_usage_directives",
            return_value=usage_directives_return,
        ),
        patch("personal_agent.orchestrator.skills.get_all_skills", return_value={}),
        patch(
            "personal_agent.llm_client.factory.get_llm_client",
            return_value=_make_mock_llm_client(_make_minimal_response()),
        ),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        ),
    )


class TestSkillNudgeInjection:
    """FRE-337: skill directive blocks appear after skill content, gated by flag."""

    @pytest.mark.asyncio
    async def test_directive_blocks_appear_after_skill_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Usage directive block lands AFTER the skill body sentinel."""
        from personal_agent.config import settings
        from personal_agent.telemetry.trace import TraceContext

        monkeypatch.setattr(settings, "cache_frozen_layout_enabled", False)
        monkeypatch.setattr(settings, "prefer_primitives_enabled", True)
        monkeypatch.setattr(settings, "skill_routing_mode", "keyword")
        monkeypatch.setattr(settings, "skill_routing_model_key", "")
        monkeypatch.setattr(settings, "skill_nudge_enabled", True)

        ctx = _make_minimal_ctx()
        trace_ctx = TraceContext.new_trace()
        mock_llm = _make_mock_llm_client(_make_minimal_response())
        mock_session = MagicMock()
        mock_session.add_message = AsyncMock()
        mock_session.get_messages = AsyncMock(return_value=[])

        with (
            patch("personal_agent.orchestrator.skills.get_skill_block", return_value=_SENTINEL),
            patch("personal_agent.orchestrator.skills.assemble_skill_index", return_value=""),
            patch(
                "personal_agent.orchestrator.skills.assemble_skill_index_directive",
                return_value="",
            ),
            patch(
                "personal_agent.orchestrator.skills.assemble_skill_usage_directives",
                return_value=_USAGE_DIRECTIVE_SENTINEL,
            ),
            patch("personal_agent.orchestrator.skills.get_all_skills", return_value={}),
            patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_llm),
            patch(
                "personal_agent.orchestrator.executor.get_default_registry",
                return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
            ),
        ):
            from personal_agent.orchestrator.executor import step_llm_call

            await step_llm_call(ctx, mock_session, trace_ctx)  # type: ignore[arg-type]

        call_kwargs = mock_llm.respond.call_args.kwargs
        system_prompt = call_kwargs.get("system_prompt", "") or ""
        assert _SENTINEL in system_prompt
        assert _USAGE_DIRECTIVE_SENTINEL in system_prompt
        assert system_prompt.index(_SENTINEL) < system_prompt.index(_USAGE_DIRECTIVE_SENTINEL)

    @pytest.mark.asyncio
    async def test_neither_directive_block_when_flag_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When skill_nudge_enabled=False, neither directive block is injected."""
        from personal_agent.config import settings
        from personal_agent.telemetry.trace import TraceContext

        monkeypatch.setattr(settings, "skill_routing_mode", "keyword")
        monkeypatch.setattr(settings, "skill_routing_model_key", "")
        monkeypatch.setattr(settings, "skill_nudge_enabled", False)

        ctx = _make_minimal_ctx()
        trace_ctx = TraceContext.new_trace()
        mock_llm = _make_mock_llm_client(_make_minimal_response())
        mock_session = MagicMock()
        mock_session.add_message = AsyncMock()
        mock_session.get_messages = AsyncMock(return_value=[])

        with (
            patch("personal_agent.orchestrator.skills.get_skill_block", return_value=_SENTINEL),
            patch("personal_agent.orchestrator.skills.assemble_skill_index", return_value=""),
            patch(
                "personal_agent.orchestrator.skills.assemble_skill_index_directive",
                return_value=_INDEX_DIRECTIVE_SENTINEL,
            ),
            patch(
                "personal_agent.orchestrator.skills.assemble_skill_usage_directives",
                return_value=_USAGE_DIRECTIVE_SENTINEL,
            ),
            patch("personal_agent.orchestrator.skills.get_all_skills", return_value={}),
            patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_llm),
            patch(
                "personal_agent.orchestrator.executor.get_default_registry",
                return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
            ),
        ):
            from personal_agent.orchestrator.executor import step_llm_call

            await step_llm_call(ctx, mock_session, trace_ctx)  # type: ignore[arg-type]

        call_kwargs = mock_llm.respond.call_args.kwargs
        system_prompt = call_kwargs.get("system_prompt", "") or ""
        assert _INDEX_DIRECTIVE_SENTINEL not in system_prompt
        assert _USAGE_DIRECTIVE_SENTINEL not in system_prompt

    @pytest.mark.asyncio
    async def test_neither_directive_block_when_no_skill_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no skill content is injected at all, no directive blocks fire."""
        from personal_agent.config import settings
        from personal_agent.telemetry.trace import TraceContext

        monkeypatch.setattr(settings, "skill_routing_mode", "keyword")
        monkeypatch.setattr(settings, "skill_routing_model_key", "")
        monkeypatch.setattr(settings, "skill_nudge_enabled", True)

        ctx = _make_minimal_ctx()
        trace_ctx = TraceContext.new_trace()
        mock_llm = _make_mock_llm_client(_make_minimal_response())
        mock_session = MagicMock()
        mock_session.add_message = AsyncMock()
        mock_session.get_messages = AsyncMock(return_value=[])

        with (
            patch("personal_agent.orchestrator.skills.get_skill_block", return_value=""),
            patch("personal_agent.orchestrator.skills.assemble_skill_index", return_value=""),
            patch(
                "personal_agent.orchestrator.skills.assemble_skill_index_directive",
                return_value=_INDEX_DIRECTIVE_SENTINEL,
            ),
            patch(
                "personal_agent.orchestrator.skills.assemble_skill_usage_directives",
                return_value=_USAGE_DIRECTIVE_SENTINEL,
            ),
            patch("personal_agent.orchestrator.skills.get_all_skills", return_value={}),
            patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_llm),
            patch(
                "personal_agent.orchestrator.executor.get_default_registry",
                return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
            ),
        ):
            from personal_agent.orchestrator.executor import step_llm_call

            await step_llm_call(ctx, mock_session, trace_ctx)  # type: ignore[arg-type]

        call_kwargs = mock_llm.respond.call_args.kwargs
        system_prompt = call_kwargs.get("system_prompt", "") or ""
        assert _INDEX_DIRECTIVE_SENTINEL not in system_prompt
        assert _USAGE_DIRECTIVE_SENTINEL not in system_prompt
