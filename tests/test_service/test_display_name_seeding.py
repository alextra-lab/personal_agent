"""Tests for FRE-344: config-driven display name seeding.

Covers:
- AppConfig.user_display_names property (JSON parsing + error cases)
- MemoryService.update_person_name_if_default (mocked Neo4j driver)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from personal_agent.memory.service import MemoryService


# ---------------------------------------------------------------------------
# AppConfig.user_display_names
# ---------------------------------------------------------------------------


def _make_settings(json_val: str):
    """Return an AppConfig instance with user_display_names_json set."""
    from personal_agent.config.settings import AppConfig

    return AppConfig(AGENT_USER_DISPLAY_NAMES_JSON=json_val)


def test_user_display_names_parses_valid_json() -> None:
    """Valid JSON map is returned as a dict."""
    settings = _make_settings('{"alice@x.com": "Alice", "bob@x.com": "Bob"}')
    assert settings.user_display_names == {"alice@x.com": "Alice", "bob@x.com": "Bob"}


def test_user_display_names_empty_default() -> None:
    """Default empty JSON returns empty dict."""
    settings = _make_settings("{}")
    assert settings.user_display_names == {}


def test_user_display_names_invalid_json_returns_empty_dict() -> None:
    """Malformed JSON is handled gracefully and returns {}."""
    settings = _make_settings("not-json")
    assert settings.user_display_names == {}


def test_user_display_names_single_entry() -> None:
    """Single-entry map parses correctly."""
    settings = _make_settings('{"solo@example.com": "Solo"}')
    assert settings.user_display_names == {"solo@example.com": "Solo"}


# ---------------------------------------------------------------------------
# MemoryService.update_person_name_if_default (mocked Neo4j)
# ---------------------------------------------------------------------------


def _make_memory_service_connected() -> MemoryService:
    """Return a MemoryService stub with connected=True and a mock driver."""
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    service.driver = MagicMock()
    return service


@pytest.mark.asyncio
async def test_update_person_name_returns_true_when_updated() -> None:
    """Returns True when Neo4j reports 1 node updated."""
    service = _make_memory_service_connected()
    uid = uuid4()

    mock_record = MagicMock()
    mock_record.__getitem__ = lambda self, key: 1  # record["updated"] == 1

    mock_result = AsyncMock()
    mock_result.single.return_value = mock_record

    mock_session = AsyncMock()
    mock_session.run.return_value = mock_result
    service.driver.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    service.driver.session.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await service.update_person_name_if_default(
        user_id=uid, current_default="alice", new_name="Alice"
    )

    assert result is True
    mock_session.run.assert_called_once()


@pytest.mark.asyncio
async def test_update_person_name_returns_false_when_no_match() -> None:
    """Returns False when the node name was already enriched (no rows updated)."""
    service = _make_memory_service_connected()
    uid = uuid4()

    mock_record = MagicMock()
    mock_record.__getitem__ = lambda self, key: 0  # record["updated"] == 0

    mock_result = AsyncMock()
    mock_result.single.return_value = mock_record

    mock_session = AsyncMock()
    mock_session.run.return_value = mock_result
    service.driver.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    service.driver.session.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await service.update_person_name_if_default(
        user_id=uid, current_default="alice", new_name="Alice"
    )

    assert result is False


@pytest.mark.asyncio
async def test_update_person_name_returns_false_when_no_record() -> None:
    """Returns False when single() returns None (node not found)."""
    service = _make_memory_service_connected()
    uid = uuid4()

    mock_result = AsyncMock()
    mock_result.single.return_value = None

    mock_session = AsyncMock()
    mock_session.run.return_value = mock_result
    service.driver.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    service.driver.session.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await service.update_person_name_if_default(
        user_id=uid, current_default="alice", new_name="Alice"
    )

    assert result is False


@pytest.mark.asyncio
async def test_update_person_name_skips_when_not_connected() -> None:
    """Returns False immediately when Neo4j is not connected."""
    service = MemoryService.__new__(MemoryService)
    service.connected = False
    service.driver = None

    result = await service.update_person_name_if_default(
        user_id=uuid4(), current_default="alice", new_name="Alice"
    )

    assert result is False


@pytest.mark.asyncio
async def test_update_person_name_handles_driver_exception() -> None:
    """Returns False and logs a warning when Neo4j raises."""
    service = _make_memory_service_connected()
    uid = uuid4()

    mock_session = AsyncMock()
    mock_session.run.side_effect = RuntimeError("Neo4j timeout")
    service.driver.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    service.driver.session.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await service.update_person_name_if_default(
        user_id=uid, current_default="alice", new_name="Alice"
    )

    assert result is False
