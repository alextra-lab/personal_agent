"""ADR-0107 D5: the streaming (SSE) chat path binds structlog.contextvars too.

Mirrors ``test_chat_contextvars_propagation.py`` for the
``_process_chat_stream_background`` fire-and-forget task, which is the SSE/AG-UI
counterpart to the non-streaming ``/chat`` endpoint and binds/clears
independently.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import structlog.contextvars
import structlog.testing

from personal_agent.service.app import _process_chat_stream_background

_TEST_USER_ID = uuid4()


@asynccontextmanager
async def _fake_db_session(_mock_db: MagicMock):
    yield _mock_db


@pytest.mark.asyncio
@patch("personal_agent.service.app._append_assistant_message_background", new_callable=AsyncMock)
@patch("personal_agent.service.app._validate_attachments", new_callable=AsyncMock)
@patch("personal_agent.transport.agui.transport.emit_done", new_callable=AsyncMock)
@patch("personal_agent.transport.agui.transport._push_event", new_callable=AsyncMock)
@patch("personal_agent.orchestrator.Orchestrator")
@patch("personal_agent.service.app.SessionRepository")
@patch("personal_agent.service.app.AsyncSessionLocal")
async def test_stream_background_binds_user_id_reaching_gateway_pipeline_logs(
    mock_session_local: MagicMock,
    mock_repo_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
    mock_push_event: AsyncMock,
    mock_emit_done: AsyncMock,
    mock_validate_attachments: AsyncMock,
    mock_append_bg: AsyncMock,
) -> None:
    """A log line from the (unmocked) gateway pipeline must carry the bound user_id."""
    session_id = uuid4()
    session = SimpleNamespace(session_id=session_id, messages=[], execution_profile="local")
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=session)
    mock_repo.append_message = AsyncMock(return_value=None)
    mock_repo_cls.return_value = mock_repo
    mock_session_local.side_effect = lambda: _fake_db_session(MagicMock())

    mock_validate_attachments.return_value = []

    session_manager = MagicMock()
    session_manager.get_session.return_value = None
    orchestrator = MagicMock()
    orchestrator.session_manager = session_manager
    orchestrator.handle_user_request = AsyncMock(
        return_value={"reply": "hi", "trace_id": "trace-1"}
    )
    mock_orchestrator_cls.return_value = orchestrator

    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as captured:
        await _process_chat_stream_background(
            session_id=str(session_id),
            message="Tell me about Python",
            user_id=_TEST_USER_ID,
            trace_id="trace-1",
        )

    gateway_log = next(e for e in captured if e.get("event") == "intent_classified")
    assert gateway_log["user_id"] == str(_TEST_USER_ID)
    assert gateway_log["session_id"] == str(session_id)

    # Request teardown must clear the binding — nothing lingers between requests.
    assert structlog.contextvars.get_contextvars() == {}
