"""Server-authoritative primary-selection resolution in /chat/stream (ADR-0121 §4, AC-6).

Covers ``_resolve_session_selection`` — the selection-store analog of
``_resolve_session_profile``. Existing sessions use the stored selection
(supplied is ignored — a stale client cannot overwrite it); a new session adopts
a valid supplied key, else bridges from the still-live profile pill (so a cloud
session is not silently created as local), else the primary default.

Fixtures use three mutually distinct catalog keys so no branch can pass by
coincidence (AC-6): A=claude_sonnet (non-default), B=claude_haiku (non-default),
D=qwen3.6-35b-thinking (the configured default / local profile primary).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from personal_agent.service.app import _resolve_session_selection

_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
_A = "claude_sonnet"  # non-default
_B = "claude_haiku"  # non-default
_D = "qwen3.6-35b-thinking"  # configured default (local profile primary_model)

_SESSIONLOCAL = "personal_agent.service.app.AsyncSessionLocal"
_SESSION_GET = "personal_agent.service.repositories.session_repository.SessionRepository.get"
_SELECTION_GET = (
    "personal_agent.service.repositories.session_model_selection_repository."
    "SessionModelSelectionRepository.get"
)
_SELECTION_UPSERT = (
    "personal_agent.service.repositories.session_model_selection_repository."
    "SessionModelSelectionRepository.upsert"
)


class _FakeACM:
    def __init__(self, db: Any) -> None:
        self._db = db

    async def __aenter__(self) -> Any:
        return self._db

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _sessionlocal_factory(db: Any) -> Any:
    return lambda: _FakeACM(db)


@pytest.mark.asyncio
async def test_existing_session_stored_wins_supplied_ignored() -> None:
    """AC-6a — existing session storing A ignores a supplied B and runs A; nothing written."""
    session = SimpleNamespace(execution_profile="cloud")
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_SESSION_GET, new_callable=AsyncMock, return_value=session),
        patch(_SELECTION_GET, new_callable=AsyncMock, return_value=_A),
        patch(_SELECTION_UPSERT, new_callable=AsyncMock) as upsert_mock,
    ):
        key, provenance = await _resolve_session_selection(
            str(uuid4()), _B, "cloud", _USER_ID, trace_id="t"
        )

    assert key == _A  # stored wins; supplied B ignored
    assert provenance == "stored"
    upsert_mock.assert_not_awaited()  # resolution never overwrites a stored selection


@pytest.mark.asyncio
async def test_new_session_adopts_valid_supplied_key() -> None:
    """AC-6b — new session supplying B runs B (persisted downstream), not the default D."""
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_SESSION_GET, new_callable=AsyncMock, return_value=None),
        patch(_SELECTION_GET, new_callable=AsyncMock, return_value=None),
    ):
        key, provenance = await _resolve_session_selection(
            str(uuid4()), _B, "local", _USER_ID, trace_id="t"
        )

    assert key == _B  # adopted supplied, NOT the local-profile default D
    assert provenance == "adopted"


@pytest.mark.asyncio
async def test_new_session_no_supply_adopts_default() -> None:
    """AC-6c — new session supplying nothing adopts the default D (via the local profile)."""
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_SESSION_GET, new_callable=AsyncMock, return_value=None),
        patch(_SELECTION_GET, new_callable=AsyncMock, return_value=None),
    ):
        key, _provenance = await _resolve_session_selection(
            str(uuid4()), None, "local", _USER_ID, trace_id="t"
        )

    assert key == _D


@pytest.mark.asyncio
async def test_new_cloud_session_bridges_to_cloud_model_not_local() -> None:
    """Codex finding 3 — new session, no key, profile=cloud → claude_sonnet, never local D.

    Proves the live Path pill keeps working: a cloud pill with no explicit model
    key resolves to the cloud primary, not the local default.
    """
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_SESSION_GET, new_callable=AsyncMock, return_value=None),
        patch(_SELECTION_GET, new_callable=AsyncMock, return_value=None),
    ):
        key, provenance = await _resolve_session_selection(
            str(uuid4()), None, "cloud", _USER_ID, trace_id="t"
        )

    assert key == _A  # claude_sonnet — the cloud profile's primary, NOT local D
    assert key != _D
    assert provenance == "profile-bridge"


@pytest.mark.asyncio
async def test_existing_session_missing_row_bridges_from_execution_profile() -> None:
    """A pre-migration existing session with no row bridges from its execution_profile."""
    session = SimpleNamespace(execution_profile="cloud")
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_SESSION_GET, new_callable=AsyncMock, return_value=session),
        patch(_SELECTION_GET, new_callable=AsyncMock, return_value=None),
    ):
        key, provenance = await _resolve_session_selection(
            str(uuid4()), _B, "local", _USER_ID, trace_id="t"
        )

    assert key == _A  # bridged from stored execution_profile='cloud', supplied B ignored
    assert provenance == "stored-bridge"


@pytest.mark.asyncio
async def test_existing_session_stale_stored_key_fails_closed_to_default() -> None:
    """A stored key no longer in the catalog fail-closes to the primary default (§6)."""
    session = SimpleNamespace(execution_profile="local")
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_SESSION_GET, new_callable=AsyncMock, return_value=session),
        patch(_SELECTION_GET, new_callable=AsyncMock, return_value="retired_model_key"),
    ):
        key, provenance = await _resolve_session_selection(
            str(uuid4()), None, "local", _USER_ID, trace_id="t"
        )

    assert key == _D  # guardrail fail-closed
    assert provenance == "stored"
