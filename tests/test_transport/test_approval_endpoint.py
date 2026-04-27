"""Tests for POST /agui/approval/{request_id} endpoint (FRE-261).

Uses FastAPI TestClient with dependency overrides to avoid requiring a live DB
or Cloudflare Access JWT.

Sync tests that need to pre-register a waiter (which requires a running event
loop) do so by running the setup inside asyncio.run().
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.service.auth import RequestUser
from personal_agent.transport.agui.approval_waiter import (
    ApprovalDecision,
    ApprovalWaiterEntry,
    _pending,
)
from personal_agent.transport.agui.endpoint import router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(user_id: UUID) -> FastAPI:
    """Build a minimal FastAPI app with the transport router and a mocked auth dep."""
    app = FastAPI()

    from personal_agent.service.auth import get_request_user  # noqa: PLC0415

    async def _mock_user() -> RequestUser:
        return RequestUser(user_id=user_id, email="test@example.com")

    app.dependency_overrides[get_request_user] = _mock_user
    app.include_router(router)
    return app


def _inject_waiter(request_id: str, session_id: str) -> asyncio.Future[ApprovalDecision]:
    """Directly inject a pre-created Future into _pending without needing a running loop.

    This avoids the ``asyncio.get_running_loop()`` requirement of
    ``register_approval_waiter`` in sync test contexts (TestClient is sync).
    """
    loop = asyncio.new_event_loop()
    fut: asyncio.Future[ApprovalDecision] = loop.create_future()
    _pending[request_id] = ApprovalWaiterEntry(future=fut, session_id=session_id)
    return fut


@pytest.fixture(autouse=True)
def _clean_pending():
    """Ensure _pending is clean around each test."""
    _pending.clear()
    yield
    _pending.clear()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_submit_approval_approve_happy_path() -> None:
    """POST /agui/approval/{id} with 'approve' resolves the waiter and returns ok."""
    user_id = uuid4()
    request_id = str(uuid4())

    fut = _inject_waiter(request_id, str(user_id))

    app = _make_app(user_id)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/approval/{request_id}",
        json={"decision": "approve", "reason": "looks safe"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    # Future should be resolved.
    assert fut.done()
    result = fut.result()
    assert result == ApprovalDecision(decision="approve", reason="looks safe")


def test_submit_approval_deny_happy_path() -> None:
    """POST /agui/approval/{id} with 'deny' resolves the waiter as denied."""
    user_id = uuid4()
    request_id = str(uuid4())

    fut = _inject_waiter(request_id, str(user_id))

    app = _make_app(user_id)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/approval/{request_id}",
        json={"decision": "deny"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert fut.result().decision == "deny"


# ---------------------------------------------------------------------------
# 404 cases
# ---------------------------------------------------------------------------


def test_submit_approval_unknown_request_id_returns_404() -> None:
    """POST /agui/approval/{unknown} returns 404."""
    user_id = uuid4()
    app = _make_app(user_id)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/approval/{uuid4()}",
        json={"decision": "approve"},
    )
    assert resp.status_code == 404


def test_submit_approval_already_resolved_returns_404() -> None:
    """POST to an already-resolved request_id returns 404."""
    user_id = uuid4()
    request_id = str(uuid4())

    fut = _inject_waiter(request_id, str(user_id))
    # Resolve it manually so the endpoint sees it as already done.
    fut.set_result(ApprovalDecision(decision="approve"))

    app = _make_app(user_id)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/approval/{request_id}",
        json={"decision": "deny"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_submit_approval_cross_session_returns_404() -> None:
    """POST /agui/approval/{id} authenticated as a different session returns 404.

    Registers the waiter under session A's user_id but submits the decision
    authenticated as session B.  resolve_approval must detect the mismatch and
    return False so the endpoint returns 404.
    """
    session_a_user_id = uuid4()
    session_b_user_id = uuid4()
    request_id = str(uuid4())

    # Waiter registered under session A.
    _inject_waiter(request_id, str(session_a_user_id))

    # Request authenticated as session B.
    app = _make_app(session_b_user_id)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/approval/{request_id}",
        json={"decision": "approve"},
    )
    assert resp.status_code == 404


def test_submit_approval_invalid_decision_returns_422() -> None:
    """POST with an invalid decision value returns 422 (Pydantic validation)."""
    user_id = uuid4()
    request_id = str(uuid4())
    _inject_waiter(request_id, str(user_id))

    app = _make_app(user_id)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/approval/{request_id}",
        json={"decision": "maybe"},
    )
    assert resp.status_code == 422
