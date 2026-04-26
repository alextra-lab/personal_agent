"""Tests for the AG-UI SSE endpoint."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from personal_agent.service.auth import RequestUser
from personal_agent.service.models import SessionModel
from personal_agent.transport.agui.endpoint import (
    _session_queues,
    cleanup_session,
    get_event_queue,
)
from personal_agent.transport.events import TextDeltaEvent

_TEST_USER_ID = uuid4()
_TEST_USER = RequestUser(user_id=_TEST_USER_ID, email="test@example.com")


def _make_test_app():
    """Create a minimal FastAPI app with auth + db dependencies overridden."""
    from fastapi import FastAPI
    from personal_agent.service.auth import get_request_user
    from personal_agent.service.database import get_db_session
    from personal_agent.transport.agui.endpoint import router

    test_app = FastAPI()
    test_app.include_router(router)

    # Override identity: always return the test user
    test_app.dependency_overrides[get_request_user] = lambda: _TEST_USER

    # Override DB: return a mock session that finds a matching session model
    mock_session_model = MagicMock(spec=SessionModel)
    mock_session_model.session_id = uuid4()
    mock_session_model.user_id = _TEST_USER_ID

    async def _mock_db_session():
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_session_model
        mock_db.execute.return_value = mock_result
        yield mock_db

    test_app.dependency_overrides[get_db_session] = _mock_db_session
    return test_app


class TestGetEventQueue:
    def setup_method(self) -> None:
        """Clear global queue state before each test."""
        _session_queues.clear()

    def test_creates_queue_for_new_session(self) -> None:
        queue = get_event_queue("session-1")
        assert queue is not None
        assert isinstance(queue, asyncio.Queue)

    def test_returns_same_queue_for_same_session(self) -> None:
        queue_a = get_event_queue("session-2")
        queue_b = get_event_queue("session-2")
        assert queue_a is queue_b

    def test_different_sessions_get_different_queues(self) -> None:
        queue_a = get_event_queue("session-a")
        queue_b = get_event_queue("session-b")
        assert queue_a is not queue_b

    def test_queue_is_initially_empty(self) -> None:
        queue = get_event_queue("session-empty")
        assert queue.empty()


class TestCleanupSession:
    def setup_method(self) -> None:
        _session_queues.clear()

    def test_removes_existing_queue(self) -> None:
        get_event_queue("session-x")
        assert "session-x" in _session_queues
        cleanup_session("session-x")
        assert "session-x" not in _session_queues

    def test_safe_when_session_not_found(self) -> None:
        # Should not raise even if session doesn't exist.
        cleanup_session("nonexistent-session")

    def test_does_not_affect_other_sessions(self) -> None:
        get_event_queue("keep-me")
        get_event_queue("remove-me")
        cleanup_session("remove-me")
        assert "keep-me" in _session_queues
        assert "remove-me" not in _session_queues


@pytest.mark.asyncio
class TestQueueRoundTrip:
    """Async tests: push an event into the queue and verify it arrives."""

    def setup_method(self) -> None:
        _session_queues.clear()

    async def test_put_and_get(self) -> None:
        event = TextDeltaEvent(text="hello", session_id="s")
        queue = get_event_queue("s")
        await queue.put(event)
        result = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert result == event

    async def test_none_sentinel(self) -> None:
        queue = get_event_queue("sentinel-test")
        await queue.put(None)
        result = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert result is None


class TestSseEndpoint:
    """Integration test for the SSE endpoint via FastAPI TestClient."""

    def setup_method(self) -> None:
        _session_queues.clear()

    def test_stream_endpoint_returns_done_for_completed_session(self) -> None:
        """Push a None sentinel before the client connects; the SSE stream should emit DONE."""
        try:
            from httpx import Client  # noqa: F401
        except ImportError:
            pytest.skip("httpx not available")

        from fastapi.testclient import TestClient

        test_app = _make_test_app()

        sid = str(uuid4())
        queue = get_event_queue(sid)
        # Pre-fill queue with a text event and then the sentinel.
        queue.put_nowait(TextDeltaEvent(text="hi", session_id=sid))
        queue.put_nowait(None)

        with TestClient(test_app, raise_server_exceptions=True) as client:
            with client.stream("GET", f"/stream/{sid}") as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]
                lines = []
                for line in resp.iter_lines():
                    lines.append(line)
                    if "DONE" in line:
                        break

        data_lines = [l for l in lines if l.startswith("data:")]
        assert len(data_lines) >= 1
        # Last data line should be DONE
        done_lines = [l for l in data_lines if "DONE" in l]
        assert done_lines, f"DONE not found in lines: {lines}"

    def test_stream_endpoint_includes_text_delta(self) -> None:
        """Verify TEXT_DELTA events are emitted before DONE."""
        try:
            from httpx import Client  # noqa: F401
        except ImportError:
            pytest.skip("httpx not available")

        from fastapi.testclient import TestClient

        test_app = _make_test_app()

        sid = str(uuid4())
        queue = get_event_queue(sid)
        queue.put_nowait(TextDeltaEvent(text="streaming", session_id=sid))
        queue.put_nowait(None)

        with TestClient(test_app, raise_server_exceptions=True) as client:
            with client.stream("GET", f"/stream/{sid}") as resp:
                lines = []
                for line in resp.iter_lines():
                    lines.append(line)
                    if "DONE" in line:
                        break

        data_lines = [l for l in lines if l.startswith("data:")]
        payloads = [json.loads(l[len("data: "):]) for l in data_lines]
        types = [p["type"] for p in payloads]
        assert "TEXT_DELTA" in types
        assert "DONE" in types
