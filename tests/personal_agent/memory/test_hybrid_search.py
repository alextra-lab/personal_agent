# tests/personal_agent/memory/test_hybrid_search.py
"""Tests for hybrid search (vector + keyword + graph traversal).

Hybrid search combines:
1. Vector similarity (embedding cosine distance)
2. Keyword matching (existing entity name/type MERGE)
3. Graph traversal (relationship-based discovery)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.memory.models import MemoryQuery
from personal_agent.memory.service import MemoryService


def _make_service() -> MemoryService:
    """Build a MemoryService instance bypassing __init__ for unit testing."""
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    service._query_feedback_by_key = {}

    mock_session = AsyncMock()
    # keyword query: result.values() → []
    keyword_result = AsyncMock()
    keyword_result.values = AsyncMock(return_value=[])
    # vector query: result.data() → []
    vector_result = AsyncMock()
    vector_result.data = AsyncMock(return_value=[])
    # entity importance query (inside _calculate_relevance_scores): async iterable
    importance_result = AsyncMock()
    importance_result.__aiter__ = MagicMock(return_value=iter([]))

    call_count = 0

    async def _run_side_effect(*args: object, **kwargs: object) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return keyword_result
        if call_count == 2:
            return vector_result
        return importance_result

    mock_session.run = AsyncMock(side_effect=_run_side_effect)

    service.driver = MagicMock()
    service.driver.session = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=mock_session),
        __aexit__=AsyncMock(return_value=None),
    ))
    return service


class TestHybridQueryMemory:
    @pytest.mark.asyncio
    async def test_vector_search_called_when_query_text_provided(self) -> None:
        """When query_text is provided, vector search should run."""
        service = _make_service()

        with patch(
            "personal_agent.memory.service.generate_embedding",
            new_callable=AsyncMock,
            return_value=[0.1] * 1536,
        ) as mock_embed:
            query = MemoryQuery(entity_names=["Redis"], limit=10)
            await service.query_memory(query, query_text="Tell me about Redis caching")
            mock_embed.assert_called_once()

    @pytest.mark.asyncio
    async def test_keyword_only_when_no_query_text(self) -> None:
        """Without query_text, only keyword search runs (backward compatible)."""
        service = _make_service()

        with patch(
            "personal_agent.memory.service.generate_embedding",
            new_callable=AsyncMock,
        ) as mock_embed:
            query = MemoryQuery(entity_names=["Redis"], limit=10)
            await service.query_memory(query)
            mock_embed.assert_not_called()
