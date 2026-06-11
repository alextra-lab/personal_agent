"""Tests for FRE-523: eval runs exercise the full cognitive pipeline.

Supersedes FRE-387. The EVAL channel now RUNS Captain's Log capture, the
``RequestCapturedEvent``, and reflection (so consolidation/entity-extraction
can write eval-derived content to the KG), while keeping outward-facing
side effects suppressed (e.g. ``create_linear_issue``) and the request-trace
ES observability doc suppressed. Eval-derived captures carry identifiable
EVAL provenance (``eval_mode=True``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from personal_agent.events.models import RequestCompletedEvent
from personal_agent.events.request_completed_handlers import (
    build_request_trace_es_handler,
    build_session_writer_handler,
)
from personal_agent.governance.models import Mode
from personal_agent.orchestrator import Channel
from personal_agent.orchestrator.executor import (
    _trigger_captains_log_reflection,
    execute_task_safe,
)
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.types import ExecutionContext
from personal_agent.telemetry.trace import TraceContext
from tests.test_orchestrator.conftest import configure_mock_llm_client_model_configs

_FAKE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Executor capture + reflection gate
# ---------------------------------------------------------------------------


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_eval_mode_now_writes_capture_and_reflection(mock_client_class: MagicMock) -> None:
    """EVAL sessions must write captures and trigger reflection (FRE-523).

    The former ``if not ctx.eval_mode:`` gate is removed: the cognitive pipeline
    (write_capture + RequestCapturedEvent + reflection) runs for eval turns so
    consolidation can extract eval content into the KG. Outward side effects stay
    suppressed elsewhere (tools/linear.py, request-trace ES handler).
    """
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {
        "role": "assistant",
        "content": "Eval response",
        "tool_calls": [],
        "reasoning_trace": None,
        "usage": {"total_tokens": 10},
        "raw": {},
    }

    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)
    trace_ctx = TraceContext.new_trace()
    ctx = ExecutionContext(
        session_id=session_id,
        trace_id=trace_ctx.trace_id,
        user_message="eval test prompt",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        eval_mode=True,
        user_id=_FAKE_USER_ID,
    )

    with (
        patch("personal_agent.captains_log.capture.write_capture") as mock_write,
        patch("personal_agent.events.bus.get_event_bus"),
        patch(
            "personal_agent.orchestrator.executor._trigger_captains_log_reflection",
            new_callable=AsyncMock,
        ) as mock_reflect,
    ):
        await execute_task_safe(ctx, session_manager)

    mock_write.assert_called_once()
    mock_reflect.assert_called_once()
    # The capture must carry EVAL provenance.
    written_capture = mock_write.call_args.args[0]
    assert written_capture.eval_mode is True


@patch("personal_agent.llm_client.factory.get_llm_client")
@pytest.mark.asyncio
async def test_non_eval_writes_capture_normally(mock_client_class: MagicMock) -> None:
    """Non-EVAL sessions must still write captures when the task completes."""
    mock_client = AsyncMock()
    configure_mock_llm_client_model_configs(mock_client)
    mock_client_class.return_value = mock_client
    mock_client.respond.return_value = {
        "role": "assistant",
        "content": "Normal response",
        "tool_calls": [],
        "reasoning_trace": None,
        "usage": {"total_tokens": 10},
        "raw": {},
    }

    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)
    trace_ctx = TraceContext.new_trace()
    ctx = ExecutionContext(
        session_id=session_id,
        trace_id=trace_ctx.trace_id,
        user_message="normal test prompt",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        eval_mode=False,
        user_id=_FAKE_USER_ID,
    )

    with (
        patch("personal_agent.captains_log.capture.write_capture") as mock_write,
        patch("personal_agent.events.bus.get_event_bus"),
    ):
        await execute_task_safe(ctx, session_manager)

    mock_write.assert_called_once()


# ---------------------------------------------------------------------------
# _trigger_captains_log_reflection early return (defense-in-depth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_runs_when_eval_mode_true() -> None:
    """_trigger_captains_log_reflection must run for eval turns (FRE-523).

    Reflection is part of the cognitive pipeline that eval runs exercise. The
    generated entry must carry EVAL provenance so the promotion pipeline can
    skip it (no Linear issues filed off eval prompts).
    """
    ctx = ExecutionContext(
        session_id="test-session",
        trace_id="test-trace",
        user_message="eval prompt",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        eval_mode=True,
    )

    mock_entry = MagicMock()
    with (
        patch(
            "personal_agent.captains_log.reflection.generate_reflection_entry",
            new_callable=AsyncMock,
            return_value=mock_entry,
        ) as mock_gen,
        patch("personal_agent.captains_log.CaptainLogManager") as mock_manager_cls,
    ):
        mock_manager = MagicMock()
        mock_manager_cls.return_value = mock_manager
        await _trigger_captains_log_reflection(ctx)

    mock_gen.assert_called_once()
    # eval provenance threaded to the reflection generator.
    assert mock_gen.call_args.kwargs.get("eval_mode") is True
    mock_manager.write_entry.assert_called_once_with(mock_entry)


@pytest.mark.asyncio
async def test_reflection_runs_when_eval_mode_false() -> None:
    """_trigger_captains_log_reflection proceeds normally when eval_mode=False."""
    ctx = ExecutionContext(
        session_id="test-session",
        trace_id="test-trace",
        user_message="real prompt",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        eval_mode=False,
    )

    mock_entry = MagicMock()
    with (
        patch(
            "personal_agent.captains_log.reflection.generate_reflection_entry",
            new_callable=AsyncMock,
            return_value=mock_entry,
        ) as mock_gen,
        patch("personal_agent.captains_log.CaptainLogManager") as mock_manager_cls,
    ):
        mock_manager = MagicMock()
        mock_manager_cls.return_value = mock_manager
        await _trigger_captains_log_reflection(ctx)

    mock_gen.assert_called_once()
    mock_manager.write_entry.assert_called_once_with(mock_entry)


# ---------------------------------------------------------------------------
# ES trace handler gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_es_trace_handler_skips_events_with_eval_mode() -> None:
    """ES trace handler must skip RequestCompletedEvent when eval_mode=True."""
    mock_es_handler = MagicMock()
    mock_es_handler._connected = True
    mock_es_handler.es_logger = AsyncMock()

    handler = build_request_trace_es_handler(mock_es_handler)

    event = RequestCompletedEvent(
        trace_id="eval-trace-123",
        session_id="eval-session-456",
        assistant_response="eval reply",
        trace_summary={},
        trace_breakdown=[],
        source_component="test",
        eval_mode=True,
    )
    await handler(event)

    mock_es_handler.es_logger.index_request_trace_from_snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_es_trace_handler_indexes_non_eval_events() -> None:
    """ES trace handler must index RequestCompletedEvent when eval_mode=False."""
    mock_es_handler = MagicMock()
    mock_es_handler._connected = True
    mock_es_handler.es_logger = AsyncMock()

    handler = build_request_trace_es_handler(mock_es_handler)

    event = RequestCompletedEvent(
        trace_id="real-trace-123",
        session_id="real-session-456",
        assistant_response="real reply",
        trace_summary={},
        trace_breakdown=[],
        source_component="test",
        eval_mode=False,
    )
    await handler(event)

    mock_es_handler.es_logger.index_request_trace_from_snapshot.assert_called_once()


# ---------------------------------------------------------------------------
# Session writer continues for eval (multi-turn eval continuity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_writer_runs_for_eval_events() -> None:
    """Session writer must append assistant messages even for eval sessions.

    Multi-turn eval requires DB persistence so subsequent turns can read
    history. eval_mode must NOT gate the session writer.
    """
    mock_repo = AsyncMock()
    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)

    handler = build_session_writer_handler()

    event = RequestCompletedEvent(
        trace_id="eval-trace-789",
        session_id=str(uuid4()),
        assistant_response="eval multi-turn reply",
        trace_summary={},
        trace_breakdown=[],
        source_component="test",
        eval_mode=True,
    )

    with (
        patch(
            "personal_agent.events.request_completed_handlers.AsyncSessionLocal",
            return_value=mock_db,
        ),
        patch(
            "personal_agent.events.request_completed_handlers.SessionRepository",
            return_value=mock_repo,
        ),
        patch(
            "personal_agent.events.request_completed_handlers.resolve_active_attribution",
            return_value=("test-model", "config/models.test.yaml"),
        ),
        patch("personal_agent.events.request_completed_handlers.release_session_write_wait"),
    ):
        await handler(event)

    mock_repo.append_message.assert_called_once()
