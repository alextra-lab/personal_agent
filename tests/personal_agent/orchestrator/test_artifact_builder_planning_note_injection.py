"""ADR-0122 §5/T6 — the planning note reaches the primary before it plans.

Functional mock tests: set ``ctx.artifact_builder_planning_note`` directly (the value
``_maybe_resolve_artifact_builder`` computes at turn start — proved separately in
``test_artifact_builder_turn_start.py``) and drive ``step_llm_call``, then inspect what
was actually sent to the LLM client. Covers both system-prompt layouts (ADR-0081 §D1):
the default head layout appends the note to ``system_prompt``; the frozen append-only
layout inlines it into the current user turn instead, so the note must be asserted in
different places depending on which layout is active — mirrors
``test_skill_injection.py``'s functional pattern.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_NOTE = "This turn's artifact builder is `claude_haiku` — output budget 4096 tokens."


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals() -> object:
    """Restore the executor's lazily-cached registry globals after each test.

    Mirrors ``test_skill_injection.py``'s fixture of the same name — patching
    ``get_default_registry`` seeds module-level caches that must not leak.
    """
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    saved_layer = _ex._tool_execution_layer
    yield
    _ex._tool_registry = saved_registry
    _ex._tool_execution_layer = saved_layer


def _make_ctx(*, planning_note: str | None) -> object:
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext

    ctx = ExecutionContext(
        session_id="test-session",
        trace_id="test-trace",
        user_message="build me a dashboard",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=[{"role": "user", "content": "build me a dashboard"}],
    )
    ctx.artifact_builder_planning_note = planning_note
    return ctx


def _make_mock_llm_client() -> MagicMock:
    mock_client = MagicMock()
    mock_client.respond = AsyncMock(
        return_value={
            "content": "ok",
            "tool_calls": [],
            "response_id": None,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    mock_client.model_configs = {}
    return mock_client


async def _run_step_llm_call(ctx: object) -> MagicMock:
    from personal_agent.telemetry.trace import TraceContext

    trace_ctx = TraceContext.new_trace()
    mock_llm = _make_mock_llm_client()
    mock_session = MagicMock()
    mock_session.add_message = AsyncMock()
    mock_session.get_messages = AsyncMock(return_value=[])

    with (
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=mock_llm),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        ),
    ):
        from personal_agent.orchestrator.executor import step_llm_call

        await step_llm_call(ctx, mock_session, trace_ctx)  # type: ignore[arg-type]

    return mock_llm


@pytest.mark.asyncio
async def test_planning_note_appended_to_system_prompt_head_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Head layout (the default): the note is appended to the system_prompt sent to the LLM."""
    from personal_agent.config import settings

    monkeypatch.setattr(settings, "cache_frozen_layout_enabled", False)

    ctx = _make_ctx(planning_note=_NOTE)
    mock_llm = await _run_step_llm_call(ctx)

    call_kwargs = mock_llm.respond.call_args.kwargs
    assert _NOTE in (call_kwargs.get("system_prompt") or "")


@pytest.mark.asyncio
async def test_no_planning_note_means_no_addition_head_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No note (no turn-start ask ran) → nothing about it appears in the system_prompt."""
    from personal_agent.config import settings

    monkeypatch.setattr(settings, "cache_frozen_layout_enabled", False)

    ctx = _make_ctx(planning_note=None)
    mock_llm = await _run_step_llm_call(ctx)

    call_kwargs = mock_llm.respond.call_args.kwargs
    assert "artifact builder" not in (call_kwargs.get("system_prompt") or "")


@pytest.mark.asyncio
async def test_planning_note_inlined_into_user_turn_frozen_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frozen append-only layout: the note rides the current user turn.

    Not the system head (ADR-0081 §D1) — so it must be asserted in the sent
    ``messages``, not ``system_prompt``.
    """
    from personal_agent.config import settings

    monkeypatch.setattr(settings, "cache_frozen_layout_enabled", True)

    ctx = _make_ctx(planning_note=_NOTE)
    mock_llm = await _run_step_llm_call(ctx)

    call_kwargs = mock_llm.respond.call_args.kwargs
    sent_messages = call_kwargs.get("messages") or []
    last_user_content = next(
        m["content"] for m in reversed(sent_messages) if m.get("role") == "user"
    )
    assert _NOTE in last_user_content
