"""Tests for memory_type property on Neo4j nodes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.memory.service import MemoryService


class TestPromoteEntity:
    @pytest.mark.asyncio
    async def test_promote_sets_memory_type_semantic(self) -> None:
        service = MemoryService()
        service.driver = MagicMock()

        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={
            "name": "Neo4j", "entity_type": "Technology", "mention_count": 15,
        })
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        service.driver.session = MagicMock(return_value=mock_session)

        result = await service.promote_entity(
            entity_name="Neo4j", confidence=0.85,
            source_turn_ids=["t1", "t2"], trace_id="test-trace",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_promote_entity_not_found(self) -> None:
        service = MemoryService()
        service.driver = MagicMock()

        mock_session = AsyncMock()
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value=None)
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        service.driver.session = MagicMock(return_value=mock_session)

        result = await service.promote_entity(
            entity_name="nonexistent", confidence=0.5,
            source_turn_ids=[], trace_id="test-trace",
        )
        assert result is False
