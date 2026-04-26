"""Tests for service/auth.py — CF Access inbound identity + dev-mode fallback."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, Request

from personal_agent.service.auth import (
    RequestUser,
    get_or_create_user_by_email,
    get_request_user,
)


# ---------------------------------------------------------------------------
# RequestUser dataclass
# ---------------------------------------------------------------------------


def test_request_user_has_user_id_and_email() -> None:
    uid = uuid4()
    user = RequestUser(user_id=uid, email="alice@example.com")
    assert user.user_id == uid
    assert user.email == "alice@example.com"


def test_request_user_is_hashable_by_user_id() -> None:
    uid = uuid4()
    user = RequestUser(user_id=uid, email="alice@example.com")
    assert user.user_id == uid


# ---------------------------------------------------------------------------
# get_or_create_user_by_email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_returns_existing_user_id() -> None:
    """Second call with the same email returns the same UUID."""
    existing_id = uuid4()
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing_id
    mock_session.execute.return_value = mock_result

    user_id = await get_or_create_user_by_email(mock_session, "alice@example.com")

    assert user_id == existing_id
    mock_session.commit.assert_not_called()  # no INSERT needed


@pytest.mark.asyncio
async def test_get_or_create_inserts_new_user_and_returns_fresh_uuid() -> None:
    """Unknown email triggers INSERT and returns a new UUID."""
    mock_session = AsyncMock()
    # First execute (SELECT): no result; second execute (INSERT RETURNING): new id
    new_id = uuid4()
    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = None
    insert_result = MagicMock()
    insert_result.scalar_one.return_value = new_id
    mock_session.execute.side_effect = [select_result, insert_result]

    user_id = await get_or_create_user_by_email(mock_session, "bob@example.com")

    assert user_id == new_id
    mock_session.commit.assert_called_once()


# ---------------------------------------------------------------------------
# get_request_user — CF Access header path
# ---------------------------------------------------------------------------


def _make_request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/sessions",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "query_string": b"",
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_extracts_user_from_cf_access_header() -> None:
    """Valid CF-Access-Authenticated-User-Email header resolves to a RequestUser."""
    uid = uuid4()
    request = _make_request({"cf-access-authenticated-user-email": "alice@example.com"})

    with (
        patch(
            "personal_agent.service.auth.get_or_create_user_by_email",
            new_callable=AsyncMock,
            return_value=uid,
        ) as mock_create,
        patch("personal_agent.service.auth._get_db_session") as mock_db,
    ):
        mock_db.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_db.return_value.__aexit__ = AsyncMock(return_value=False)

        user = await get_request_user(request)

    assert user.email == "alice@example.com"
    assert user.user_id == uid


@pytest.mark.asyncio
async def test_dev_mode_fallback_when_no_cf_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CF header + gateway_auth_enabled=False → deployment owner identity."""
    owner_id = uuid4()
    monkeypatch.setattr("personal_agent.service.auth.settings.gateway_auth_enabled", False)
    monkeypatch.setattr(
        "personal_agent.service.auth.settings.agent_owner_email", "owner@example.com"
    )

    request = _make_request({})  # no CF header

    with (
        patch(
            "personal_agent.service.auth.get_or_create_user_by_email",
            new_callable=AsyncMock,
            return_value=owner_id,
        ),
        patch("personal_agent.service.auth._get_db_session") as mock_db,
    ):
        mock_db.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_db.return_value.__aexit__ = AsyncMock(return_value=False)

        user = await get_request_user(request)

    assert user.email == "owner@example.com"
    assert user.user_id == owner_id


@pytest.mark.asyncio
async def test_raises_401_when_no_cf_header_and_auth_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No CF header + gateway_auth_enabled=True → 401."""
    monkeypatch.setattr("personal_agent.service.auth.settings.gateway_auth_enabled", True)
    monkeypatch.setattr("personal_agent.service.auth.settings.agent_owner_email", None)

    request = _make_request({})

    with pytest.raises(HTTPException) as exc_info:
        await get_request_user(request)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_raises_401_when_no_owner_email_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No CF header + gateway_auth_enabled=False but no owner email → 401."""
    monkeypatch.setattr("personal_agent.service.auth.settings.gateway_auth_enabled", False)
    monkeypatch.setattr("personal_agent.service.auth.settings.agent_owner_email", None)

    request = _make_request({})

    with pytest.raises(HTTPException) as exc_info:
        await get_request_user(request)

    assert exc_info.value.status_code == 401
