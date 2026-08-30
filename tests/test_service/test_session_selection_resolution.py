"""Server-authoritative primary-selection resolution in /chat/stream (ADR-0121 §4, AC-6).

Covers ``_resolve_session_selection``. ADR-0121 T5 (FRE-920) removed Path — the
old profile-bridge fallback for a session with no selection row is gone; a
missing row now degrades straight to the ``primary`` binding default.

Existing sessions use the stored selection (supplied is ignored — a stale
client cannot overwrite it); a new session adopts a valid supplied key, else
the primary binding default.

Fixtures use two mutually distinct catalog keys so no branch can pass by
coincidence (AC-6): A=claude_sonnet (non-default), D=qwen3.6-35b-thinking (the
configured ``primary`` binding default).
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
_D = "qwen3.6-35b-thinking"  # configured default (primary binding)

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
    """AC-6a — existing session storing A ignores a supplied D and runs A; nothing written."""
    session = SimpleNamespace()
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_SESSION_GET, new_callable=AsyncMock, return_value=session),
        patch(_SELECTION_GET, new_callable=AsyncMock, return_value=_A),
        patch(_SELECTION_UPSERT, new_callable=AsyncMock) as upsert_mock,
    ):
        key, provenance = await _resolve_session_selection(str(uuid4()), _D, _USER_ID, trace_id="t")

    assert key == _A  # stored wins; supplied D ignored
    assert provenance == "stored"
    upsert_mock.assert_not_awaited()  # resolution never overwrites a stored selection


@pytest.mark.asyncio
async def test_new_session_adopts_valid_supplied_key() -> None:
    """AC-6b — new session supplying A runs A (persisted downstream), not the default D."""
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_SESSION_GET, new_callable=AsyncMock, return_value=None),
        patch(_SELECTION_GET, new_callable=AsyncMock, return_value=None),
    ):
        key, provenance = await _resolve_session_selection(str(uuid4()), _A, _USER_ID, trace_id="t")

    assert key == _A  # adopted supplied, NOT the binding default D
    assert provenance == "adopted"


@pytest.mark.asyncio
async def test_new_session_no_supply_adopts_default() -> None:
    """AC-6c — new session supplying nothing adopts the binding default D."""
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_SESSION_GET, new_callable=AsyncMock, return_value=None),
        patch(_SELECTION_GET, new_callable=AsyncMock, return_value=None),
    ):
        key, provenance = await _resolve_session_selection(
            str(uuid4()), None, _USER_ID, trace_id="t"
        )

    assert key == _D
    assert provenance == "default"


@pytest.mark.asyncio
async def test_existing_session_missing_row_falls_back_to_default() -> None:
    """ADR-0121 T5: a pre-migration existing session with no row degrades to the
    binding default — the retired profile bridge no longer papers over this.
    """
    session = SimpleNamespace()
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_SESSION_GET, new_callable=AsyncMock, return_value=session),
        patch(_SELECTION_GET, new_callable=AsyncMock, return_value=None),
    ):
        key, provenance = await _resolve_session_selection(str(uuid4()), _A, _USER_ID, trace_id="t")

    assert key == _D  # binding default, supplied A ignored (existing session)
    assert provenance == "default"


@pytest.mark.asyncio
async def test_existing_session_stale_stored_key_fails_closed_to_default() -> None:
    """A stored key no longer in the catalog fail-closes to the primary default (§6)."""
    session = SimpleNamespace()
    with (
        patch(_SESSIONLOCAL, _sessionlocal_factory(object())),
        patch(_SESSION_GET, new_callable=AsyncMock, return_value=session),
        patch(_SELECTION_GET, new_callable=AsyncMock, return_value="retired_model_key"),
    ):
        key, provenance = await _resolve_session_selection(
            str(uuid4()), None, _USER_ID, trace_id="t"
        )

    assert key == _D  # guardrail fail-closed
    assert provenance == "stored"
