"""Session management for orchestrator.

This module provides session storage and retrieval for multi-turn conversations.
Sessions are stored in-memory for MVP, with optional JSON persistence for recovery.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel


@dataclass
class Session:
    """A single conversation session.

    Attributes:
        session_id: Unique identifier for this session.
        mode: Operational mode for this session.
        channel: Communication channel (CHAT, CODE_TASK, SYSTEM_HEALTH).
        messages: OpenAI-style chat history (system, user, assistant, tool).
        metadata: Additional session metadata (user preferences, etc.).
        created_at: UTC timestamp when session was created.
        last_active_at: UTC timestamp of last activity.
    """

    session_id: str
    mode: Mode
    channel: Channel
    messages: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_active_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class SessionManager:
    """Manages session lifecycle and storage.

    Stores sessions in-memory for fast access. Optionally persists to disk
    on shutdown and loads on startup (future enhancement).

    For MVP, sessions are stored in a simple dict. Future versions may add:
    - JSON persistence for recovery
    - Automatic archiving of stale sessions
    - Session expiration and cleanup
    """

    def __init__(self) -> None:
        """Initialize session manager with empty in-memory store."""
        self._sessions: dict[str, Session] = {}

    def create_session(self, mode: Mode, channel: Channel, session_id: str | None = None) -> str:
        """Create a new session and return its ID.

        Args:
            mode: Operational mode for the session.
            channel: Communication channel.
            session_id: Optional session ID. If None, generates a UUID.

        Returns:
            The session_id (UUID string or provided ID).

        Raises:
            ValueError: If session_id is provided and already exists.
        """
        if session_id is None:
            session_id = str(uuid.uuid4())
        elif session_id in self._sessions:
            raise ValueError(f"Session {session_id} already exists")

        session = Session(
            session_id=session_id,
            mode=mode,
            channel=channel,
        )
        self._sessions[session_id] = session
        return session_id

    def get_session(self, session_id: str) -> Session | None:
        """Retrieve a session by ID.

        Args:
            session_id: The session identifier.

        Returns:
            The Session object, or None if not found.
        """
        session = self._sessions.get(session_id)
        if session:
            session.last_active_at = datetime.now(UTC)
        return session

    def update_session(self, session_id: str, messages: list[dict[str, Any]] | None = None) -> None:
        """Update session conversation history.

        Args:
            session_id: The session identifier.
            messages: Optional new messages list to replace existing messages.
                If None, only updates last_active_at timestamp.

        Raises:
            ValueError: If session_id not found.
        """
        session = self._sessions.get(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")
        if messages is not None:
            session.messages = messages
        session.last_active_at = datetime.now(UTC)

    def list_active_sessions(self) -> list[Session]:
        """List all active sessions.

        Returns:
            List of all Session objects, sorted by last_active_at (newest first).
        """
        return sorted(
            list(self._sessions.values()),
            key=lambda s: s.last_active_at,
            reverse=True,
        )

    def delete_session(self, session_id: str) -> None:
        """Delete a session.

        Args:
            session_id: The session identifier to delete.

        Raises:
            ValueError: If session_id not found.
        """
        if session_id not in self._sessions:
            raise ValueError(f"Session {session_id} not found")
        del self._sessions[session_id]
