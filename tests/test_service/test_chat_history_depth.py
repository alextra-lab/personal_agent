"""FRE-1427 AC-4 — conversation_max_history_messages actually gates the turn.

The setting moved 10 -> 20 alongside the qwen3.8-flash-next window shrinking
262144 -> 131072 (freed KV buys history depth). Reading the setting only proves
a number changed; this proves the truncation the setting drives still runs
inside ``chat()``'s real session-hydration phase and reaches the gateway
pipeline with the new depth.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.config import get_settings
from personal_agent.service.app import chat
from personal_agent.service.auth import RequestUser

_TEST_REQUEST_USER = RequestUser(user_id=uuid4(), email="test@example.com")


def _make_history(count: int) -> list[dict[str, str]]:
    return [{"role": "user", "content": f"turn {i}"} for i in range(count)]


@pytest.mark.asyncio
@patch("personal_agent.orchestrator.Orchestrator")
@patch("personal_agent.service.app.SessionRepository")
@patch("personal_agent.service.app.run_gateway_pipeline", new_callable=AsyncMock)
async def test_session_hydration_truncates_to_the_configured_history_depth(
    mock_pipeline: AsyncMock,
    mock_repo_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
) -> None:
    """25 stored messages hydrate into the pipeline call truncated to the setting's depth."""
    max_history = get_settings().conversation_max_history_messages
    assert max_history == 20, "AC-4 fixture assumes FRE-1427's new depth; update if it moves again"

    stored_messages = _make_history(25)
    session_id = uuid4()
    session = SimpleNamespace(
        session_id=session_id, messages=stored_messages, execution_profile="local"
    )
    mock_repo = MagicMock()
    mock_repo.get = AsyncMock(return_value=session)
    mock_repo.append_message = AsyncMock(return_value=None)
    mock_repo_cls.return_value = mock_repo

    mock_pipeline.return_value = None

    session_manager = MagicMock()
    session_manager.get_session.return_value = None
    orchestrator = MagicMock()
    orchestrator.session_manager = session_manager
    orchestrator.handle_user_request = AsyncMock(
        return_value={"reply": "hi", "trace_id": "trace-1"}
    )
    mock_orchestrator_cls.return_value = orchestrator

    await chat(
        message="what did we discuss",
        session_id=str(session_id),
        request_user=_TEST_REQUEST_USER,
        db=AsyncMock(),
    )

    mock_pipeline.assert_called_once()
    hydrated_messages = mock_pipeline.call_args.kwargs["session_messages"]
    assert len(hydrated_messages) == 20
    # The tail 20 of the 25, not an arbitrary 20 — proves the slice, not just the count.
    assert hydrated_messages == stored_messages[-20:]
