"""Tests for POST /approval/{request_id} endpoint (FRE-261 + FRE-378).

Uses FastAPI TestClient with dependency overrides to avoid requiring a live DB
or Cloudflare Access JWT.

FRE-378 fix: the endpoint now requires the caller to include ``session_id`` in
the request body, verifies session ownership against the authenticated user
via ``SessionRepository.get``, and passes the session_id (not user_id) to
``resolve_approval()``. Tests both the happy path and the security boundaries.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.service.auth import RequestUser
from personal_agent.transport.agui.approval_waiter import (
    ApprovalDecision,
    _pending,
    register_approval_waiter,
)
from personal_agent.transport.agui.endpoint import router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(
    user_id: UUID,
    *,
    owns_session: bool = True,
) -> FastAPI:
    """Build a minimal FastAPI app with the transport router and mocked deps.

    Args:
        user_id: The user identity returned by the auth dependency.
        owns_session: When True, the mocked ``SessionRepository.get`` returns
            a truthy session object regardless of the requested session_id —
            simulating the calling user owning whatever they ask for. When
            False, ``get`` returns ``None`` — simulating "session not owned
            by this user" (the cross-user attack we want to 404 on).
    """
    app = FastAPI()

    from personal_agent.service.auth import get_request_user  # noqa: PLC0415
    from personal_agent.service.database import get_db_session  # noqa: PLC0415

    async def _mock_user() -> RequestUser:
        return RequestUser(user_id=user_id, email="test@example.com")

    async def _mock_db() -> Any:
        # SessionRepository(db).get(...) is the only DB call in the endpoint;
        # patching the method directly on the class is the cleanest path.
        return MagicMock(spec=AsyncSession)

    app.dependency_overrides[get_request_user] = _mock_user
    app.dependency_overrides[get_db_session] = _mock_db

    # Patch SessionRepository.get to encode the ownership flag.
    from personal_agent.service.repositories.session_repository import (  # noqa: PLC0415
        SessionRepository,
    )

    sentinel_session = MagicMock() if owns_session else None
    SessionRepository.get = AsyncMock(return_value=sentinel_session)  # type: ignore[method-assign]

    app.include_router(router)
    return app


def _inject_waiter(request_id: str, session_id: str) -> asyncio.Future[ApprovalDecision]:
    """Register a waiter using the canonical registration function.

    Runs ``register_approval_waiter`` inside ``asyncio.run()`` so it can call
    ``asyncio.get_running_loop()`` internally. The returned Future is valid
    for synchronous ``.set_result()`` / ``.result()`` calls even after the
    temporary loop is closed.
    """

    async def _register() -> asyncio.Future[ApprovalDecision]:
        return register_approval_waiter(request_id, session_id)

    return asyncio.run(_register())


@pytest.fixture(autouse=True)
def _clean_pending() -> Any:
    """Ensure _pending is clean around each test."""
    _pending.clear()
    yield
    _pending.clear()


# ---------------------------------------------------------------------------
# Happy path — session_id in body, owned by caller, waiter matches
# ---------------------------------------------------------------------------


def test_submit_approval_approve_happy_path() -> None:
    """POST /approval/{id} with matching session_id resolves the waiter."""
    user_id = uuid4()
    session_id = str(uuid4())
    request_id = str(uuid4())

    fut = _inject_waiter(request_id, session_id)

    app = _make_app(user_id, owns_session=True)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/approval/{request_id}",
        json={
            "session_id": session_id,
            "decision": "approve",
            "reason": "looks safe",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert fut.done()
    assert fut.result() == ApprovalDecision(decision="approve", reason="looks safe")


def test_submit_approval_deny_happy_path() -> None:
    """POST /approval/{id} with 'deny' resolves the waiter as denied."""
    user_id = uuid4()
    session_id = str(uuid4())
    request_id = str(uuid4())

    fut = _inject_waiter(request_id, session_id)

    app = _make_app(user_id, owns_session=True)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/approval/{request_id}",
        json={"session_id": session_id, "decision": "deny"},
    )
    assert resp.status_code == 200
    assert fut.result().decision == "deny"


# ---------------------------------------------------------------------------
# 404 cases — request_id unknown / already resolved / cross-session
# ---------------------------------------------------------------------------


def test_submit_approval_unknown_request_id_returns_404() -> None:
    """POST /approval/{unknown} returns 404."""
    user_id = uuid4()
    app = _make_app(user_id, owns_session=True)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/approval/{uuid4()}",
        json={"session_id": str(uuid4()), "decision": "approve"},
    )
    assert resp.status_code == 404


def test_submit_approval_already_resolved_returns_404() -> None:
    """POST to an already-resolved request_id returns 404."""
    user_id = uuid4()
    session_id = str(uuid4())
    request_id = str(uuid4())

    fut = _inject_waiter(request_id, session_id)
    fut.set_result(ApprovalDecision(decision="approve"))

    app = _make_app(user_id, owns_session=True)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/approval/{request_id}",
        json={"session_id": session_id, "decision": "deny"},
    )
    assert resp.status_code == 404


def test_submit_approval_session_owned_by_other_user_returns_404() -> None:
    """Caller submits a session_id they don't own → 404, no resolve attempted.

    FRE-378 security boundary: even if the caller knows a valid request_id
    and the session_id it was registered under, they cannot resolve it as
    a different user. ``repo.get(sid, user_id=B)`` returns None → 404.
    """
    user_id = uuid4()  # caller B
    other_session = str(uuid4())  # owned by user A
    request_id = str(uuid4())

    fut = _inject_waiter(request_id, other_session)

    app = _make_app(user_id, owns_session=False)  # caller does NOT own that session
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/approval/{request_id}",
        json={"session_id": other_session, "decision": "approve"},
    )
    assert resp.status_code == 404
    # Waiter must NOT have been resolved.
    assert not fut.done()


def test_submit_approval_wrong_session_for_request_returns_404() -> None:
    """Caller owns session A but the request_id was registered for session B → 404."""
    user_id = uuid4()
    session_a = str(uuid4())  # caller owns this
    session_b = str(uuid4())  # request_id registered under this
    request_id = str(uuid4())

    fut = _inject_waiter(request_id, session_b)

    app = _make_app(user_id, owns_session=True)  # caller owns session_a (mocked True)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/approval/{request_id}",
        json={"session_id": session_a, "decision": "approve"},
    )
    assert resp.status_code == 404
    assert not fut.done()


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_submit_approval_missing_session_id_returns_422() -> None:
    """POST without session_id returns 422 (Pydantic validation)."""
    user_id = uuid4()
    request_id = str(uuid4())
    _inject_waiter(request_id, str(uuid4()))

    app = _make_app(user_id, owns_session=True)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/approval/{request_id}",
        json={"decision": "approve"},  # missing session_id
    )
    assert resp.status_code == 422


def test_submit_approval_malformed_session_id_returns_422() -> None:
    """POST with a non-UUID session_id returns 422."""
    user_id = uuid4()
    request_id = str(uuid4())

    app = _make_app(user_id, owns_session=True)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/approval/{request_id}",
        json={"session_id": "not-a-uuid", "decision": "approve"},
    )
    assert resp.status_code == 422


def test_submit_approval_invalid_decision_returns_422() -> None:
    """POST with an invalid decision value returns 422 (Pydantic validation)."""
    user_id = uuid4()
    session_id = str(uuid4())
    request_id = str(uuid4())
    _inject_waiter(request_id, session_id)

    app = _make_app(user_id, owns_session=True)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/approval/{request_id}",
        json={"session_id": session_id, "decision": "maybe"},
    )
    assert resp.status_code == 422
