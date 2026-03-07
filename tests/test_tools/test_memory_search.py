"""Tests for search_memory tool (ADR-0026)."""

import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.memory.models import MemoryQueryResult, TurnNode
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.memory_search import (
    _extract_keywords,
    _looks_like_broad_query,
    search_memory_executor,
    search_memory_tool,
)


def _fake_app_module(memory_service: MagicMock | None = None) -> MagicMock:
    """Build a fake personal_agent.service.app module to avoid importing real app (and its deps)."""
    fake = MagicMock()
    fake.memory_service = memory_service
    return fake


def test_search_memory_tool_definition() -> None:
    """Test search_memory tool has expected definition and parameters."""
    assert search_memory_tool.name == "search_memory"
    assert "memory graph" in search_memory_tool.description
    param_names = {p.name for p in search_memory_tool.parameters}
    assert "query_text" in param_names
    assert "entity_types" in param_names
    assert "entity_names" in param_names
    assert "recency_days" in param_names
    assert "limit" in param_names
    assert search_memory_tool.risk_level == "low"
    assert "NORMAL" in search_memory_tool.allowed_modes


def test_looks_like_broad_query() -> None:
    """Test broad-query heuristic."""
    assert _looks_like_broad_query("what have I discussed before?", []) is True
    assert _looks_like_broad_query("anything about travel", []) is True
    assert _looks_like_broad_query("past topics", []) is True
    assert _looks_like_broad_query("Athens", []) is False
    assert _looks_like_broad_query("what have I discussed?", ["Location"]) is False


def test_extract_keywords() -> None:
    """Test keyword extraction from free text (capitalised words, len > 2)."""
    assert _extract_keywords("We talked about Athens and Santorini") == ["Athens", "Santorini"]
    assert _extract_keywords("Python async patterns") == ["Python"]
    assert _extract_keywords("no caps here") == []
    assert len(_extract_keywords("A B C D E F")) <= 5


@pytest.mark.asyncio
async def test_search_memory_executor_not_connected_raises() -> None:
    """Test search_memory_executor raises when memory service is not connected."""
    fake_app = _fake_app_module(memory_service=None)
    with patch.dict(sys.modules, {"personal_agent.service.app": fake_app}):
        with pytest.raises(ToolExecutionError) as exc_info:
            await search_memory_executor(query_text="Athens")
        msg = str(exc_info.value).lower()
        assert "unavailable" in msg or "not connected" in msg


@pytest.mark.asyncio
async def test_search_memory_executor_entity_path_returns_matched_turns() -> None:
    """Test search_memory_executor returns matched_turns on entity-match path."""
    turn = TurnNode(
        turn_id="turn-1",
        timestamp=datetime.now(timezone.utc),
        user_message="I want to visit Athens",
        summary="User asked about Athens",
        key_entities=["Athens"],
    )
    query_result = MemoryQueryResult(conversations=[turn], entities=[])

    mock_service = MagicMock()
    mock_service.connected = True
    mock_service.query_memory = AsyncMock(return_value=query_result)

    with patch.dict(sys.modules, {"personal_agent.service.app": _fake_app_module(mock_service)}):
        result = await search_memory_executor(
            query_text="Athens",
            entity_names=["Athens"],
        )
    assert "matched_turns" in result
    assert result["query_path"] == "entity_match"
    assert len(result["matched_turns"]) == 1
    assert result["matched_turns"][0]["turn_id"] == "turn-1"
    assert "Athens" in result["matched_turns"][0]["user_message"]


@pytest.mark.asyncio
async def test_search_memory_executor_broad_path_returns_entities() -> None:
    """Test search_memory_executor returns entities/sessions on broad-recall path."""
    broad_result = {
        "entities": [{"name": "Athens", "type": "Location", "mentions": 2}],
        "sessions": [],
        "turns_summary": [{"summary": "Talked about Greece", "ts": "2026-01-01T00:00:00"}],
    }

    mock_service = MagicMock()
    mock_service.connected = True
    mock_service.query_memory_broad = AsyncMock(return_value=broad_result)

    with patch.dict(sys.modules, {"personal_agent.service.app": _fake_app_module(mock_service)}):
        result = await search_memory_executor(
            query_text="what have I discussed before?",
        )
    assert result["query_path"] == "broad_recall"
    assert "entities" in result
    assert "recent_turns" in result
    assert len(result["entities"]) == 1
    assert result["entities"][0]["name"] == "Athens"
