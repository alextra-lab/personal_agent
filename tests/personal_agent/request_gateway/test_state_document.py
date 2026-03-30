"""Tests for structured context assembly — state document builder."""

from __future__ import annotations

from typing import Any

from personal_agent.request_gateway.state_document import (
    _MIN_TURNS_FOR_STATE_DOC,
    build_state_document,
)


def _msg(role: str, content: str) -> dict[str, Any]:
    return {"role": role, "content": content}


class TestBuildStateDocument:
    def test_returns_none_for_short_sessions(self) -> None:
        messages = [_msg("user", "Hello"), _msg("assistant", "Hi")]
        assert build_state_document(messages) is None

    def test_returns_none_for_empty_sessions(self) -> None:
        assert build_state_document([]) is None

    def test_returns_doc_for_sufficient_turns(self) -> None:
        messages = [
            _msg("user", "Build a REST API for task management"),
            _msg("assistant", "I'll help you build that. What framework?"),
            _msg("user", "Let's use FastAPI with PostgreSQL"),
            _msg("assistant", "Great choices. I'll set up the project structure."),
        ]
        doc = build_state_document(messages)
        assert doc is not None
        assert "## Current Session State" in doc
        assert "**Goal:**" in doc

    def test_extracts_goal_from_first_user_message(self) -> None:
        messages = [
            _msg("user", "Build a REST API for task management"),
            _msg("assistant", "Sure."),
            _msg("user", "Add authentication"),
            _msg("assistant", "OK."),
        ]
        doc = build_state_document(messages)
        assert doc is not None
        assert "Build a REST API for task management" in doc

    def test_extracts_constraints_from_decisions(self) -> None:
        messages = [
            _msg("user", "What framework should we use?"),
            _msg("assistant", "I recommend FastAPI."),
            _msg("user", "Let's go with FastAPI and PostgreSQL for the database."),
            _msg("assistant", "Good choice."),
        ]
        doc = build_state_document(messages)
        assert doc is not None
        assert "**Constraints:**" in doc
        assert "FastAPI" in doc

    def test_extracts_recent_actions(self) -> None:
        messages = [
            _msg("user", "Start with the models"),
            _msg("assistant", "Created the User model in models.py"),
            _msg("user", "Now add the routes"),
            _msg("assistant", "Set up CRUD routes in routes/tasks.py"),
        ]
        doc = build_state_document(messages)
        assert doc is not None
        assert "**Recent Actions:**" in doc

    def test_truncates_long_goals(self) -> None:
        long_msg = "x" * 300
        messages = [
            _msg("user", long_msg),
            _msg("assistant", "OK"),
            _msg("user", "more"),
            _msg("assistant", "sure"),
        ]
        doc = build_state_document(messages)
        assert doc is not None
        assert len(doc) < len(long_msg)

    def test_min_turns_constant(self) -> None:
        assert _MIN_TURNS_FOR_STATE_DOC == 3
