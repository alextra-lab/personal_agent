"""Tests for server-authoritative profile resolution in /chat/stream (ADR-0079 / FRE-416).

Covers ``_resolve_session_profile`` — the helper that resolves the execution
profile for a turn from the session row, killing the silent ``local`` default
and validating/persisting an explicit override.
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
_EMIT = "personal_agent.transport.agui.transport.emit_session_profile"
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
async def test_absent_param_uses_stored_profile() -> None:
    """No supplied profile → the session's stored value (never silent local)."""
    stored = SimpleNamespace(execution_profile="cloud")
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_GET, new_callable=AsyncMock, return_value=stored),
        patch(_UPDATE, new_callable=AsyncMock) as update_mock,
        patch(_EMIT, new_callable=AsyncMock) as emit_mock,
    ):
        resolved = await _resolve_session_profile(str(uuid4()), None, _USER_ID, trace_id="t")

    assert resolved == "cloud"
    update_mock.assert_not_awaited()
    emit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_absent_param_no_session_defaults_local() -> None:
    """No supplied profile and no session row yet → explicit 'local'."""
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_GET, new_callable=AsyncMock, return_value=None),
        patch(_UPDATE, new_callable=AsyncMock) as update_mock,
        patch(_EMIT, new_callable=AsyncMock),
    ):
        resolved = await _resolve_session_profile(str(uuid4()), None, _USER_ID, trace_id="t")

    assert resolved == "local"
    update_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_change_persists_and_emits() -> None:
    """A supplied profile differing from stored → persisted (scoped) + emitted."""
    stored = SimpleNamespace(execution_profile="local")
    sid = str(uuid4())
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_GET, new_callable=AsyncMock, return_value=stored),
        patch(_UPDATE, new_callable=AsyncMock) as update_mock,
        patch(_EMIT, new_callable=AsyncMock) as emit_mock,
    ):
        resolved = await _resolve_session_profile(sid, "cloud", _USER_ID, trace_id="t")

    assert resolved == "cloud"
    update_mock.assert_awaited_once()
    assert update_mock.await_args.kwargs.get("user_id") == _USER_ID
    emit_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_unchanged_does_not_write() -> None:
    """A supplied profile equal to stored → no redundant write/emit."""
    stored = SimpleNamespace(execution_profile="cloud")
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_GET, new_callable=AsyncMock, return_value=stored),
        patch(_UPDATE, new_callable=AsyncMock) as update_mock,
        patch(_EMIT, new_callable=AsyncMock) as emit_mock,
    ):
        resolved = await _resolve_session_profile(str(uuid4()), "cloud", _USER_ID, trace_id="t")

    assert resolved == "cloud"
    update_mock.assert_not_awaited()
    emit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_profile_raises_422() -> None:
    """An unknown supplied profile is rejected with 422 before any DB work."""
    with patch(_SESSIONLOCAL, _sessionlocal_factory(object())):
        with pytest.raises(HTTPException) as exc:
            await _resolve_session_profile(str(uuid4()), "bogus", _USER_ID, trace_id="t")

    assert exc.value.status_code == 422
