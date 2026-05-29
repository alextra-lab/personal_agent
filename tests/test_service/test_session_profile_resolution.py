"""Tests for server-authoritative profile resolution in /chat/stream (ADR-0079 / FRE-416/419).

Covers ``_resolve_session_profile`` — the helper that resolves the execution
profile for a turn. Existing sessions use the stored value (supplied is
ignored); a brand-new session adopts the client's supplied pill (so a new
"Cloud" session is not silently created as ``local``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from personal_agent.service.app import _resolve_session_profile

_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
_GET = "personal_agent.service.repositories.session_repository.SessionRepository.get"
_UPDATE = "personal_agent.service.repositories.session_repository.SessionRepository.update"
_SESSIONLOCAL = "personal_agent.service.app.AsyncSessionLocal"


class _FakeACM:
    """Minimal async context manager yielding a fake DB handle."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def __aenter__(self) -> Any:
        return self._db

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _sessionlocal_factory(db: Any) -> Any:
    """Return a callable that mimics ``AsyncSessionLocal()`` → async CM."""
    return lambda: _FakeACM(db)


@pytest.mark.asyncio
async def test_existing_session_uses_stored_when_param_absent() -> None:
    """Existing session, no supplied profile → the stored value (no silent local)."""
    stored = SimpleNamespace(execution_profile="cloud")
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_GET, new_callable=AsyncMock, return_value=stored),
        patch(_UPDATE, new_callable=AsyncMock) as update_mock,
    ):
        resolved = await _resolve_session_profile(str(uuid4()), None, _USER_ID, trace_id="t")

    assert resolved == "cloud"
    update_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_session_ignores_supplied() -> None:
    """Existing session: a supplied value is advisory and ignored (stored wins).

    This is what keeps the original cloud→local desync fixed — a stale/reloaded
    client cannot overwrite the stored profile via /chat/stream (PATCH only).
    """
    stored = SimpleNamespace(execution_profile="cloud")
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_GET, new_callable=AsyncMock, return_value=stored),
        patch(_UPDATE, new_callable=AsyncMock) as update_mock,
    ):
        resolved = await _resolve_session_profile(str(uuid4()), "local", _USER_ID, trace_id="t")

    assert resolved == "cloud"
    update_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_session_adopts_supplied() -> None:
    """New session (no row) → adopt the client's supplied pill (FRE-419 fix)."""
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_GET, new_callable=AsyncMock, return_value=None),
        patch(_UPDATE, new_callable=AsyncMock) as update_mock,
    ):
        resolved = await _resolve_session_profile(str(uuid4()), "cloud", _USER_ID, trace_id="t")

    assert resolved == "cloud"
    update_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_session_no_param_defaults_local() -> None:
    """New session with nothing supplied → explicit 'local' fallback."""
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_GET, new_callable=AsyncMock, return_value=None),
        patch(_UPDATE, new_callable=AsyncMock),
    ):
        resolved = await _resolve_session_profile(str(uuid4()), None, _USER_ID, trace_id="t")

    assert resolved == "local"


@pytest.mark.asyncio
async def test_invalid_profile_raises_422() -> None:
    """An unknown supplied profile is rejected with 422 before any DB work."""
    with patch(_SESSIONLOCAL, _sessionlocal_factory(object())):
        with pytest.raises(HTTPException) as exc:
            await _resolve_session_profile(str(uuid4()), "bogus", _USER_ID, trace_id="t")

    assert exc.value.status_code == 422
