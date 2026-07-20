"""ADR-0107 D5: /chat binds structlog.contextvars for the request's lifetime.

The gateway pipeline runs for real in these tests (not mocked) — only the
orchestrator and repository layer are mocked, matching the existing
``test_chat_hydration.py`` convention — so a log line it emits (e.g.
``intent_classified``) is a genuine, unmocked witness that the contextvars
bind at the top of ``chat()`` actually reaches downstream call sites, not
just the log call at the binding site itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import structlog.contextvars
import structlog.testing

from personal_agent.service.app import chat
from personal_agent.service.auth import RequestUser

_TEST_USER_ID = uuid4()
_TEST_REQUEST_USER = RequestUser(user_id=_TEST_USER_ID, email="test@example.com")


@pytest.mark.asyncio
@patch("personal_agent.orchestrator.Orchestrator")
@patch("personal_agent.service.app.SessionRepository")
async def test_chat_binds_user_id_reaching_gateway_pipeline_logs(
    mock_repo_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
) -> None:
    """A log line from the (unmocked) gateway pipeline must carry the bound user_id."""
    session_id = uuid4()
    session = SimpleNamespace(session_id=session_id, messages=[], execution_profile="local")
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=session)
    mock_repo.create = AsyncMock(return_value=session)
    mock_repo.append_message = AsyncMock(return_value=None)
    mock_repo_cls.return_value = mock_repo

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
        await chat(
            message="Tell me about Python",
            session_id=str(session_id),
            request_user=_TEST_REQUEST_USER,
            db=AsyncMock(),
        )

    gateway_log = next(e for e in captured if e.get("event") == "intent_classified")
    assert gateway_log["user_id"] == str(_TEST_USER_ID)
    assert gateway_log["session_id"] == str(session_id)

    # Request teardown must clear the binding — nothing lingers between requests.
    assert structlog.contextvars.get_contextvars() == {}


@pytest.mark.asyncio
@patch("personal_agent.orchestrator.Orchestrator")
@patch("personal_agent.service.app.SessionRepository")
async def test_chat_rebinds_real_session_id_for_a_new_session(
    mock_repo_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
) -> None:
    """A new-session request (session_id=None) must not bind session_id=None
    for the request's lifetime — code-review finding: chat() bound the raw,
    still-None session_id parameter before _chat_impl created the real
    session, so gateway pipeline logs carried session_id: null.
    """
    new_session_id = uuid4()
    session = SimpleNamespace(session_id=new_session_id, messages=[], execution_profile="local")
    mock_repo = MagicMock()
    mock_repo.create = AsyncMock(return_value=session)
    # ADR-0121 §4: selection resolution re-fetches by session_id (the session
    # now exists, just created above) — matches the other tests in this file.
    mock_repo.get = AsyncMock(return_value=session)
    mock_repo.append_message = AsyncMock(return_value=None)
    mock_repo_cls.return_value = mock_repo

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
        await chat(
            message="Tell me about Python",
            session_id=None,
            request_user=_TEST_REQUEST_USER,
            db=AsyncMock(),
        )

    gateway_log = next(e for e in captured if e.get("event") == "intent_classified")
    assert gateway_log["session_id"] == str(new_session_id)


@pytest.mark.asyncio
@patch("personal_agent.orchestrator.Orchestrator")
@patch("personal_agent.service.app.SessionRepository")
async def test_chat_clears_context_even_when_orchestrator_raises(
    mock_repo_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
) -> None:
    """The clear must run on the error path too (`finally`, not just the happy path)."""
    session_id = uuid4()
    session = SimpleNamespace(session_id=session_id, messages=[], execution_profile="local")
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=session)
    mock_repo.create = AsyncMock(return_value=session)
    mock_repo.append_message = AsyncMock(return_value=None)
    mock_repo_cls.return_value = mock_repo

    session_manager = MagicMock()
    session_manager.get_session.return_value = None
    orchestrator = MagicMock()
    orchestrator.session_manager = session_manager
    orchestrator.handle_user_request = AsyncMock(side_effect=RuntimeError("boom"))
    mock_orchestrator_cls.return_value = orchestrator

    await chat(
        message="Tell me about Python",
        session_id=str(session_id),
        request_user=_TEST_REQUEST_USER,
        db=AsyncMock(),
    )

    assert structlog.contextvars.get_contextvars() == {}
