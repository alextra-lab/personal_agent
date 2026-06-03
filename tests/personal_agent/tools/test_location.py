"""Tests for FRE-230 location tool."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from personal_agent.config import settings
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.location import ClientCoordinatesProvider, ExplicitLocationProvider

_CTX = TraceContext.new_trace()


def test_opt_out_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    """When location is disabled, get_location is not registered.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(settings, "location_enabled", False)

    from personal_agent.tools import register_mvp_tools
    from personal_agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    register_mvp_tools(registry)

    assert "get_location" not in registry.list_tool_names()


@pytest.mark.asyncio
async def test_explicit_provider_parses_lisbon() -> None:
    """Explicit provider parses a user-stated Lisbon location."""
    result = await ExplicitLocationProvider("I am in Lisbon today").resolve(_CTX)

    assert result is not None
    assert result.city == "Lisbon"
    assert result.timezone is None
    assert result.latitude is None
    assert result.longitude is None


@pytest.mark.asyncio
async def test_client_provider_precise_keeps_coords() -> None:
    """Precise client provider keeps full device coordinates."""
    result = await ClientCoordinatesProvider(
        38.7077507,
        -9.1365919,
        "Europe/Lisbon",
        "precise",
    ).resolve(_CTX)

    assert result.latitude == 38.7077507
    assert result.longitude == -9.1365919
    assert result.precise is True


@pytest.mark.asyncio
async def test_client_provider_coarse_truncates_coords() -> None:
    """Coarse client provider rounds coordinates to two decimals."""
    result = await ClientCoordinatesProvider(
        38.7077507,
        -9.1365919,
        "Europe/Lisbon",
        "coarse",
    ).resolve(_CTX)

    assert result.latitude == 38.71
    assert result.longitude == -9.14
    assert result.precise is False


@pytest.mark.asyncio
async def test_client_provider_passes_timezone() -> None:
    """Client provider propagates the browser-provided timezone."""
    result = await ClientCoordinatesProvider(
        38.7077507,
        -9.1365919,
        "Europe/Lisbon",
        "precise",
    ).resolve(_CTX)

    assert result.timezone == "Europe/Lisbon"


@pytest.mark.asyncio
async def test_executor_consent_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Executor returns consent_not_given when the user has not opted in.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    from personal_agent.service import app as service_app
    from personal_agent.tools.location import get_location_executor

    user_id = uuid4()
    ctx = TraceContext.new_trace(user_id=user_id)
    mock_memory = AsyncMock()
    mock_memory.connected = True
    mock_memory.get_person_location_consent = AsyncMock(return_value=False)
    monkeypatch.setattr(settings, "location_enabled", True)
    monkeypatch.setattr(service_app, "memory_service", mock_memory)

    result = await get_location_executor("I am in Lisbon today", ctx=ctx)

    assert result == {"resolved": False, "reason": "consent_not_given"}
    mock_memory.get_person_location_consent.assert_awaited_once_with(str(user_id), ctx.trace_id)


@pytest.mark.asyncio
async def test_patch_returns_403_when_operator_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PATCH endpoint refuses with 403 when the operator gate is off.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    from fastapi import HTTPException

    from personal_agent.service.app import update_location_preference
    from personal_agent.service.auth import RequestUser
    from personal_agent.service.models import LocationPreferenceUpdate

    monkeypatch.setattr(settings, "location_enabled", False)
    request_user = RequestUser(user_id=uuid4(), email="owner@example.com")
    payload = LocationPreferenceUpdate(consent_enabled=True, latitude=38.7, longitude=-9.1)

    with pytest.raises(HTTPException) as exc_info:
        await update_location_preference(payload, request_user=request_user)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_patch_does_not_store_coords_without_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coordinates are never stored when the user withholds consent.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    from personal_agent.service import app as service_app
    from personal_agent.service.auth import RequestUser
    from personal_agent.service.models import LocationPreferenceUpdate

    monkeypatch.setattr(settings, "location_enabled", True)
    mock_memory = AsyncMock()
    mock_memory.connected = True
    mock_memory.set_person_location_consent = AsyncMock(return_value=None)
    mock_memory.update_person_location = AsyncMock(return_value=None)
    monkeypatch.setattr(service_app, "memory_service", mock_memory)

    request_user = RequestUser(user_id=uuid4(), email="owner@example.com")
    payload = LocationPreferenceUpdate(consent_enabled=False, latitude=38.7, longitude=-9.1)

    result = await service_app.update_location_preference(payload, request_user=request_user)

    assert result["location_consent_enabled"] is False
    mock_memory.update_person_location.assert_not_awaited()
