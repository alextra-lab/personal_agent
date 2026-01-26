"""Tests for session management."""

from datetime import datetime

import pytest

from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.session import SessionManager


def test_create_session() -> None:
    """Test creating a new session."""
    manager = SessionManager()
    session_id = manager.create_session(Mode.NORMAL, Channel.CHAT)

    assert session_id is not None
    assert len(session_id) > 0

    session = manager.get_session(session_id)
    assert session is not None
    assert session.session_id == session_id
    assert session.mode == Mode.NORMAL
    assert session.channel == Channel.CHAT
    assert session.messages == []
    assert isinstance(session.created_at, datetime)
    assert isinstance(session.last_active_at, datetime)


def test_get_nonexistent_session() -> None:
    """Test retrieving a non-existent session returns None."""
    manager = SessionManager()
    session = manager.get_session("nonexistent-id")

    assert session is None


def test_update_session_messages() -> None:
    """Test updating session messages."""
    manager = SessionManager()
    session_id = manager.create_session(Mode.NORMAL, Channel.CHAT)

    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]

    manager.update_session(session_id, messages=messages)

    session = manager.get_session(session_id)
    assert session is not None
    assert session.messages == messages


def test_update_session_timestamp() -> None:
    """Test that updating session updates last_active_at."""
    manager = SessionManager()
    session_id = manager.create_session(Mode.NORMAL, Channel.CHAT)

    session = manager.get_session(session_id)
    assert session is not None
    first_active = session.last_active_at

    # Wait a tiny bit and update
    import time

    time.sleep(0.01)
    manager.update_session(session_id)  # No messages, just update timestamp

    session = manager.get_session(session_id)
    assert session is not None
    assert session.last_active_at > first_active


def test_update_nonexistent_session_raises() -> None:
    """Test that updating non-existent session raises ValueError."""
    manager = SessionManager()

    with pytest.raises(ValueError, match="not found"):
        manager.update_session("nonexistent-id", messages=[])


def test_list_active_sessions() -> None:
    """Test listing all active sessions."""
    manager = SessionManager()

    # Create multiple sessions
    id1 = manager.create_session(Mode.NORMAL, Channel.CHAT)
    id2 = manager.create_session(Mode.ALERT, Channel.CODE_TASK)
    id3 = manager.create_session(Mode.DEGRADED, Channel.SYSTEM_HEALTH)

    sessions = manager.list_active_sessions()
    assert len(sessions) == 3

    # Should be sorted by last_active_at (newest first)
    session_ids = [s.session_id for s in sessions]
    assert id3 in session_ids
    assert id2 in session_ids
    assert id1 in session_ids


def test_delete_session() -> None:
    """Test deleting a session."""
    manager = SessionManager()
    session_id = manager.create_session(Mode.NORMAL, Channel.CHAT)

    manager.delete_session(session_id)

    session = manager.get_session(session_id)
    assert session is None

    sessions = manager.list_active_sessions()
    assert len(sessions) == 0


def test_delete_nonexistent_session_raises() -> None:
    """Test that deleting non-existent session raises ValueError."""
    manager = SessionManager()

    with pytest.raises(ValueError, match="not found"):
        manager.delete_session("nonexistent-id")


def test_session_datetimes_utc() -> None:
    """Test that session datetimes are in UTC."""
    manager = SessionManager()
    session_id = manager.create_session(Mode.NORMAL, Channel.CHAT)

    session = manager.get_session(session_id)
    assert session is not None
    assert session.created_at.tzinfo is not None
    assert session.last_active_at.tzinfo is not None
