"""suggest_proactive_raw stops swallowing its own DB failure (FRE-1481, ADR-0148 D1).

Confirmed single caller: MemoryServiceAdapter.suggest_relevant (see
test_proactive.py::test_failure_fallback_empty_suggestions for the end-to-end,
site-specific-cause assertion). This pins the service method's own contract change:
a DB failure now raises rather than returning ``[]``, the same value it returns on an
honest zero-row query.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.memory.service import MemoryService


def _service_with_mock() -> tuple[MemoryService, AsyncMock]:
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    service._query_feedback_by_key = {}

    mock_session = AsyncMock()
    service.driver = MagicMock()
    service.driver.session = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    return service, mock_session


class TestRaisingQueryIsReported:
    @pytest.mark.asyncio
    async def test_a_raising_db_call_propagates_rather_than_returning_empty(self) -> None:
        service, mock_session = _service_with_mock()
        mock_session.run = AsyncMock(side_effect=RuntimeError("neo4j unreachable"))

        with pytest.raises(RuntimeError, match="neo4j unreachable"):
            await service.suggest_proactive_raw([0.1] * 8, "session-1", "trace-1")

    @pytest.mark.asyncio
    async def test_a_healthy_query_still_returns_rows(self) -> None:
        """The companion: without it, the case above could pass for the wrong reason."""
        service, mock_session = _service_with_mock()

        async def fake_run(*_a: object, **_k: object) -> AsyncMock:
            r = AsyncMock()
            r.data = AsyncMock(
                return_value=[
                    {
                        "name": "Neo4j",
                        "entity_type": "Technology",
                        "description": "graph db",
                        "mention_count": 1,
                        "vector_score": 0.9,
                        "turn_id": None,
                        "session_id": None,
                        "timestamp": None,
                        "user_message": None,
                        "summary": None,
                        "key_entities": [],
                    }
                ]
            )
            return r

        mock_session.run = AsyncMock(side_effect=fake_run)

        rows = await service.suggest_proactive_raw([0.1] * 8, "session-1", "trace-1")

        assert [r["name"] for r in rows] == ["Neo4j"]


class TestGatingIsNotAFailure:
    """Disconnected / no-embedding stay legitimate no-op preconditions, not failures."""

    @pytest.mark.asyncio
    async def test_disconnected_returns_empty_without_raising(self) -> None:
        service, _ = _service_with_mock()
        service.connected = False

        assert await service.suggest_proactive_raw([0.1] * 8, "session-1", "trace-1") == []

    @pytest.mark.asyncio
    async def test_a_zero_vector_embedding_returns_empty_without_raising(self) -> None:
        service, _ = _service_with_mock()

        assert await service.suggest_proactive_raw([0.0] * 8, "session-1", "trace-1") == []
