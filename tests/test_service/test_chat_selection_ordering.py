"""FRE-978 — /chat must set the primary selection contextvar before Stage 7 runs.

Regression guard for the ordering bug this ticket fixed: ``_chat_impl`` used
to call ``run_gateway_pipeline`` *before* resolving and setting the per-turn
``primary`` selection contextvar, so Stage 7's budget trim (also FRE-978)
silently resolved against whatever the contextvar held before this request —
empty, or a stale value from elsewhere in the same task — instead of this
session's actual selection. ``/chat/stream`` (``_process_chat_stream_background``)
never had this bug: it sets the selection at the very top, before its
pipeline call. This test proves ``/chat`` now matches that ordering.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.config.selection import get_current_selection
from personal_agent.service.app import chat
from personal_agent.service.auth import RequestUser

_TEST_USER_ID = uuid4()
_TEST_REQUEST_USER = RequestUser(user_id=_TEST_USER_ID, email="test@example.com")


@pytest.mark.asyncio
@patch("personal_agent.service.app.run_gateway_pipeline")
@patch("personal_agent.orchestrator.Orchestrator")
@patch("personal_agent.service.app.SessionRepository")
async def test_chat_sets_selection_before_gateway_pipeline_runs(
    mock_repo_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
    mock_run_pipeline: AsyncMock,
) -> None:
    """Selection is set before Stage 7 runs.

    ``get_current_selection("primary")`` must already be resolved (not None)
    by the time ``run_gateway_pipeline`` is invoked.
    """
    session_id = uuid4()
    session = SimpleNamespace(session_id=session_id, messages=[], execution_profile="local")
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=session)
    mock_repo.append_message = AsyncMock(return_value=None)
    mock_repo_cls.return_value = mock_repo

    captured_selection: list[str | None] = []

    async def _capture_selection_and_return(*_args: object, **_kwargs: object) -> MagicMock:
        captured_selection.append(get_current_selection("primary"))
        return MagicMock()

    mock_run_pipeline.side_effect = _capture_selection_and_return

    session_manager = MagicMock()
    session_manager.get_session.return_value = None
    orchestrator = MagicMock()
    orchestrator.session_manager = session_manager
    orchestrator.handle_user_request = AsyncMock(
        return_value={"reply": "hi", "trace_id": "trace-1"}
    )
    mock_orchestrator_cls.return_value = orchestrator

    await chat(
        message="Tell me about Python",
        session_id=str(session_id),
        request_user=_TEST_REQUEST_USER,
        db=AsyncMock(),
    )

    assert mock_run_pipeline.await_count == 1
    assert captured_selection == [captured_selection[0]]
    assert captured_selection[0] is not None
