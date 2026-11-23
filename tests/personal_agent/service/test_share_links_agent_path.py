"""The agent's ``bash`` cannot create a share link (ADR-0153 Amendment 1 L10, V-L10 part 1).

A real gateway process serves the share-links router on 127.0.0.1, with the real
``get_request_user`` and ``gateway_auth_enabled`` on. The real ``bash`` tool runs through
``ToolExecutionLayer``, and the approval card that the call raises is approved. Its child holds the gateway's environment (the ``local`` profile,
with no credential isolation) and sends a ``curl`` POST to the create endpoint with every
gateway credential in that environment: the internal token, the plaintext identity header with
the owner's email, and a ``Cf-Access-Jwt-Assertion`` header built from them. The gateway records
that the request reached ``get_request_user``, answers 401, and no row exists afterwards.

Marker: ``integration`` — the row check reads the test Postgres (``make test-infra-up``).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from personal_agent.config import settings
from personal_agent.config.governance_loader import load_governance_config
from personal_agent.governance.models import Mode
from personal_agent.service import auth, share_links_router
from personal_agent.service.auth import RequestUser, get_request_user
from personal_agent.service.cf_access_jwt import CFAccessClaims, CFAccessVerifierError
from personal_agent.service.database import get_db_session
from personal_agent.telemetry import TraceContext
from personal_agent.tools import executor as executor_module
from personal_agent.tools.executor import ToolExecutionLayer
from personal_agent.tools.primitives import bash as bash_module
from personal_agent.tools.primitives.bash import bash_executor, bash_tool
from personal_agent.tools.registry import ToolRegistry
from personal_agent.transport.agui.ws_endpoint import ApprovalDecision
from tests._helpers.permissions import use_rules

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_OWNER = "fre1782-agentpath-owner@example.com"
_INTERNAL = "fre1782-agentpath-internal-token"


class _RejectingVerifier:
    """A configured main-app verifier. No test JWT verifies."""

    async def verify(self, token: str) -> CFAccessClaims:
        raise CFAccessVerifierError("not a real JWT")


@pytest.fixture
def no_phase_events() -> Iterator[None]:
    """Replace ``phase_span`` with a no-op so the tool layer writes no phase rows."""

    @asynccontextmanager
    async def _noop(**_kw: Any) -> AsyncIterator[None]:
        yield None

    with patch("personal_agent.transport.agui.transport.phase_span", _noop):
        yield


@pytest_asyncio.fixture
async def owner_item() -> AsyncIterator[tuple[UUID, async_sessionmaker[AsyncSession]]]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as db:
            if (
                await db.execute(text("SELECT to_regclass('artifact_share_links')"))
            ).scalar_one() is None:
                pytest.skip("artifact_share_links missing; apply migration 0042 to the test stack")
            user_id = await auth.get_or_create_user_by_email(db, _OWNER)
            item = uuid4()
            await db.execute(
                text(
                    "INSERT INTO artifacts (id, user_id, type, content_type, size_bytes, r2_key,"
                    " created_by) VALUES (:i, :u, 'upload', 'image/png', 1, :k, 'user')"
                ),
                {"i": item, "u": user_id, "k": f"upload/{user_id}/GLOBAL/{item}.png"},
            )
            await db.commit()
    except pytest.skip.Exception:
        await engine.dispose()
        raise
    except Exception as exc:  # noqa: BLE001 - environment guard
        await engine.dispose()
        pytest.skip(f"test-stack Postgres unavailable ({exc}); run `make test-infra-up`")
    try:
        yield item, maker
    finally:
        admin = create_async_engine(settings.database_admin_url, poolclass=NullPool)
        async with async_sessionmaker(admin)() as db:
            await db.execute(
                text("DELETE FROM artifact_share_links WHERE created_by = :u"), {"u": user_id}
            )
            await db.execute(text("DELETE FROM artifacts WHERE user_id = :u"), {"u": user_id})
            await db.execute(text("DELETE FROM users WHERE user_id = :u"), {"u": user_id})
            await db.commit()
        await admin.dispose()
        await engine.dispose()


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, list[str]]]:
    """Serve the share-links router on 127.0.0.1 in a thread. Yield its port and the reach log."""
    monkeypatch.setattr(settings, "gateway_auth_enabled", True)
    monkeypatch.setattr(settings, "agent_owner_email", _OWNER)
    monkeypatch.setattr(settings, "share_public_base_url", "https://share.example.com")
    monkeypatch.setattr(settings, "artifact_resolve_internal_token", _INTERNAL)
    monkeypatch.setattr(auth, "get_main_app_verifier", lambda: _RejectingVerifier())

    reached: list[str] = []
    app = FastAPI()
    app.include_router(share_links_router.router)

    async def _recording_user(request: Request) -> RequestUser:
        reached.append(request.url.path)
        return await auth.get_request_user(request)

    async def _db() -> AsyncIterator[AsyncSession]:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine)() as session:
                yield session
        finally:
            await engine.dispose()

    app.dependency_overrides[get_request_user] = _recording_user
    app.dependency_overrides[get_db_session] = _db

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the test gateway did not start"
        time.sleep(0.05)
    try:
        yield port, reached
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _approving_transport() -> MagicMock:
    """An approval channel that approves every card."""
    transport = MagicMock()
    transport.request_tool_approval = AsyncMock(return_value=ApprovalDecision(decision="approve"))
    return transport


def _layer(transport: MagicMock) -> ToolExecutionLayer:
    registry = ToolRegistry()
    registry.register(bash_tool, bash_executor)
    mode_manager = MagicMock()
    mode_manager.get_current_mode.return_value = Mode.NORMAL
    return ToolExecutionLayer(
        registry=registry,
        governance_config=load_governance_config(),
        mode_manager=mode_manager,
        transport=transport,  # type: ignore[arg-type]
    )


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
@pytest.mark.usefixtures("no_phase_events")
async def test_a_bash_call_with_every_credential_gets_401_and_creates_no_row(
    gateway: tuple[int, list[str]],
    owner_item: tuple[UUID, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, reached = gateway
    item, maker = owner_item
    use_rules(monkeypatch)
    monkeypatch.setattr(executor_module.settings, "approval_ui_enabled", True)
    monkeypatch.setenv("AGENT_ARTIFACT_RESOLVE_INTERNAL_TOKEN", _INTERNAL)
    monkeypatch.setenv("AGENT_OWNER_EMAIL", _OWNER)
    monkeypatch.setattr(bash_module.settings, "deployment_profile", "local")
    path = f"/api/v1/artifacts/{item}/share-links"
    command = (
        "curl -sS -o /dev/null -w '%{http_code}' -X POST"
        " -H 'Content-Type: application/json'"
        ' -H "X-Internal-Token: $AGENT_ARTIFACT_RESOLVE_INTERNAL_TOKEN"'
        ' -H "Cf-Access-Authenticated-User-Email: $AGENT_OWNER_EMAIL"'
        ' -H "Cf-Access-Jwt-Assertion: $AGENT_ARTIFACT_RESOLVE_INTERNAL_TOKEN"'
        """ -d '{"expires_in_days": 7}'"""
        f" http://127.0.0.1:{port}{path}"
    )

    transport = _approving_transport()
    result = await _layer(transport).execute_tool(
        "bash", {"command": command}, TraceContext.new_trace(), session_id="fre1782-agent-path"
    )

    assert result.success, result.error
    transport.request_tool_approval.assert_awaited_once()
    output = result.output if isinstance(result.output, dict) else json.loads(result.output)
    assert output["stdout"].strip() == "401", output
    assert reached == [path], "the request did not reach get_request_user"
    await asyncio.sleep(0)
    async with maker() as db:
        rows = (
            await db.execute(
                text("SELECT count(*) FROM artifact_share_links WHERE artifact_id = :i"),
                {"i": item},
            )
        ).scalar_one()
    assert rows == 0
