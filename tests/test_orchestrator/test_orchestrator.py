"""Tests for high-level orchestrator API."""

import pytest

from personal_agent.governance.models import Mode
from personal_agent.orchestrator import Channel, Orchestrator


@pytest.mark.asyncio
async def test_handle_user_request() -> None:
    """Test the high-level handle_user_request API."""
    orchestrator = Orchestrator()

    result = await orchestrator.handle_user_request(
        session_id="test-session",
        user_message="Hello, how are you?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )

    # Verify result structure
    assert "reply" in result
    assert "steps" in result
    assert "trace_id" in result

    # Verify reply exists
    assert len(result["reply"]) > 0

    # Verify steps were recorded
    assert len(result["steps"]) > 0


@pytest.mark.asyncio
async def test_handle_user_request_creates_session() -> None:
    """Test that handle_user_request creates session if it doesn't exist."""
    orchestrator = Orchestrator()

    session_id = "new-session-id"
    result = await orchestrator.handle_user_request(
        session_id=session_id,
        user_message="Test message",
        mode=Mode.NORMAL,
        channel=Channel.CODE_TASK,
    )

    # Verify session was created
    session = orchestrator.session_manager.get_session(session_id)
    assert session is not None
    assert session.mode == Mode.NORMAL
    assert session.channel == Channel.CODE_TASK

    # Verify result
    assert result["trace_id"] is not None


@pytest.mark.asyncio
async def test_handle_user_request_uses_existing_session() -> None:
    """Test that handle_user_request uses existing session."""
    orchestrator = Orchestrator()

    # Create session first
    session_id = orchestrator.session_manager.create_session(Mode.ALERT, Channel.SYSTEM_HEALTH)

    result = await orchestrator.handle_user_request(
        session_id=session_id,
        user_message="Another message",
        mode=Mode.ALERT,  # Should use existing session's mode
        channel=Channel.SYSTEM_HEALTH,
    )

    # Verify session exists and was used
    session = orchestrator.session_manager.get_session(session_id)
    assert session is not None
    assert len(session.messages) > 0  # Should have messages from execution

    # Verify result
    assert result["trace_id"] is not None
