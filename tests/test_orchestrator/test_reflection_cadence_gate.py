"""Executor-level tests for the Captain's Log reflection cadence gate (FRE-710).

`tests/test_captains_log/test_reflection_cadence.py` unit-tests `ReflectionCadenceGate` in
isolation. These tests prove the gate is actually wired into `execute_task`'s reflection call site —
closing the gap between "the gate says yes/no" and "the executor actually calls
`_trigger_captains_log_reflection`" — for the ticket's three acceptance criteria:

- AC-1: reflection no longer fires on every turn.
- AC-2: a session with real content still produces a reflection (first-turn-always-reflects).
- AC-3: proposals that mattered under per-turn still surface (the hit_iteration_limit bypass).

Plus the rollback lever (`captains_log_reflection_cadence_enabled=False`) that reverts to
unconditional per-turn reflection.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from personal_agent.captains_log.reflection_cadence import reset_reflection_cadence_gate
from personal_agent.config import settings
from personal_agent.governance.models import Mode
from personal_agent.orchestrator import Channel
from personal_agent.orchestrator.executor import execute_task_safe
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.types import ExecutionContext
from personal_agent.telemetry.trace import TraceContext
from tests.test_orchestrator.conftest import configure_mock_llm_client_model_configs

_FAKE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def _make_ctx(session_id: str, *, tool_iteration_count: int = 0) -> ExecutionContext:
    trace_ctx = TraceContext.new_trace()
    return ExecutionContext(
        session_id=session_id,
        trace_id=trace_ctx.trace_id,
        user_message="a real turn",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        eval_mode=False,
        user_id=_FAKE_USER_ID,
        tool_iteration_count=tool_iteration_count,
    )


def _configure_mock_client(mock_client_class: MagicMock) -> None:
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {
        "role": "assistant",
        "content": "a reply",
        "tool_calls": [],
        "reasoning_trace": None,
        "usage": {"total_tokens": 10},
        "raw": {},
    }


@pytest.fixture(autouse=True)
def _reset_gate() -> None:
    reset_reflection_cadence_gate()


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_first_turn_for_a_new_session_schedules_reflection(
    mock_client_class: MagicMock,
) -> None:
    """AC-2: a session with real content still produces a reflection."""
    _configure_mock_client(mock_client_class)
    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)
    ctx = _make_ctx(session_id)

    with (
        patch("personal_agent.captains_log.capture.write_capture"),
        patch("personal_agent.events.bus.get_event_bus"),
        patch(
            "personal_agent.orchestrator.executor._trigger_captains_log_reflection",
            new_callable=AsyncMock,
        ) as mock_reflect,
    ):
        await execute_task_safe(ctx, session_manager)

    mock_reflect.assert_called_once()


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_second_turn_within_the_interval_does_not_reschedule_reflection(
    mock_client_class: MagicMock,
) -> None:
    """AC-1: reflection no longer fires on every turn."""
    _configure_mock_client(mock_client_class)
    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)

    with (
        patch("personal_agent.captains_log.capture.write_capture"),
        patch("personal_agent.events.bus.get_event_bus"),
        patch(
            "personal_agent.orchestrator.executor._trigger_captains_log_reflection",
            new_callable=AsyncMock,
        ) as mock_reflect,
    ):
        await execute_task_safe(_make_ctx(session_id), session_manager)
        await execute_task_safe(_make_ctx(session_id), session_manager)

    mock_reflect.assert_called_once()


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_hit_iteration_limit_bypasses_the_interval(mock_client_class: MagicMock) -> None:
    """AC-3: proposals that mattered under per-turn still surface (iteration-limit bypass)."""
    _configure_mock_client(mock_client_class)
    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)
    over_limit = settings.orchestrator_max_tool_iterations + 1

    with (
        patch("personal_agent.captains_log.capture.write_capture"),
        patch("personal_agent.events.bus.get_event_bus"),
        patch(
            "personal_agent.orchestrator.executor._trigger_captains_log_reflection",
            new_callable=AsyncMock,
        ) as mock_reflect,
    ):
        # First turn reflects (first-turn-always-reflects).
        await execute_task_safe(_make_ctx(session_id), session_manager)
        # Second turn, well within the debounce interval, but it hit the iteration limit.
        await execute_task_safe(
            _make_ctx(session_id, tool_iteration_count=over_limit), session_manager
        )

    assert mock_reflect.call_count == 2


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_cadence_disabled_reverts_to_unconditional_per_turn_reflection(
    mock_client_class: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback lever: cadence_enabled=False preserves the pre-FRE-710 behavior."""
    monkeypatch.setattr(settings, "captains_log_reflection_cadence_enabled", False)
    _configure_mock_client(mock_client_class)
    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)

    with (
        patch("personal_agent.captains_log.capture.write_capture"),
        patch("personal_agent.events.bus.get_event_bus"),
        patch(
            "personal_agent.orchestrator.executor._trigger_captains_log_reflection",
            new_callable=AsyncMock,
        ) as mock_reflect,
    ):
        await execute_task_safe(_make_ctx(session_id), session_manager)
        await execute_task_safe(_make_ctx(session_id), session_manager)

    assert mock_reflect.call_count == 2
