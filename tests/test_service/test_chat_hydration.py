"""Service chat tests for session hydration behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.service.app import chat


@pytest.mark.asyncio
@patch("personal_agent.orchestrator.Orchestrator")
@patch("personal_agent.service.app.SessionRepository")
async def test_chat_hydrates_prior_messages_before_current_turn(
    mock_repo_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
) -> None:
    """Hydration should load only prior DB history into orchestrator memory."""
    session_id = uuid4()
    prior_messages = [
        {"role": "user", "content": "My name is Alex"},
        {"role": "assistant", "content": "Nice to meet you, Alex."},
    ]

    session = SimpleNamespace(session_id=session_id, messages=prior_messages)
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
        return_value={"reply": "Alex", "trace_id": "trace-1"}
    )
    mock_orchestrator_cls.return_value = orchestrator

    result = await chat(message="What is my name?", session_id=str(session_id), db=AsyncMock())

    assert result["response"] == "Alex"
    session_manager.update_session.assert_called_once_with(str(session_id), messages=prior_messages)
    mock_repo.append_message.assert_any_call(
        session_id,
        {"role": "user", "content": "What is my name?"},
    )
