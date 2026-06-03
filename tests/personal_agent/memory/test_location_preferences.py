"""Tests for FRE-230 location preference memory methods."""

from unittest.mock import AsyncMock

import pytest

from personal_agent.memory.service import MemoryService


def _make_service_with_mock() -> tuple[MemoryService, AsyncMock]:
    """Build a MemoryService whose driver yields a mock async session."""
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    mock_driver = AsyncMock()
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_driver.session = lambda: mock_session
    service.driver = mock_driver
    return service, mock_session


@pytest.mark.asyncio
async def test_get_person_location_consent_false_when_person_absent() -> None:
    """Missing :Person nodes default to no location consent."""
    service, mock_session = _make_service_with_mock()
    result_mock = AsyncMock()
    result_mock.single = AsyncMock(return_value=None)
    mock_session.run = AsyncMock(return_value=result_mock)

    consent = await service.get_person_location_consent("user-1", "trace-1")

    assert consent is False


@pytest.mark.asyncio
async def test_update_person_location_matches_person_and_threads_trace() -> None:
    """update_person_location MATCHes the Person and threads trace_id onto edges."""
    service, mock_session = _make_service_with_mock()
    mock_session.run = AsyncMock()

    await service.update_person_location(
        user_id="user-1",
        latitude=38.7077507,
        longitude=-9.1365919,
        timezone="Europe/Lisbon",
        source="client",
        trace_id="trace-xyz",
    )

    mock_session.run.assert_awaited_once()
    query = mock_session.run.await_args.args[0]
    params = mock_session.run.await_args.kwargs
    # MATCH (not MERGE) on Person — never spawn a bare identity node.
    assert "MATCH (p:Person {user_id: $user_id})" in query
    # Spatial point is set for distance queries.
    assert "point({latitude: $latitude, longitude: $longitude})" in query
    # CURRENTLY_AT is singular: old edge deleted before the new one.
    assert "DELETE old" in query
    # ADR-0074 identity threading: trace_id rides the edges.
    assert "c.trace_id = $trace_id" in query
    assert "v.trace_id = $trace_id" in query
    assert params["trace_id"] == "trace-xyz"
