"""Tests for search_memory tool (ADR-0026)."""

import re
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


def test_search_memory_entity_types_schema_is_v2_taxonomy() -> None:
    """ADR-0109: entity_types description advertises exactly the V2 10-type vocabulary (FRE-794)."""
    entity_types_param = next(p for p in search_memory_tool.parameters if p.name == "entity_types")
    description = entity_types_param.description
    v2_types = {
        "Person",
        "Organization",
        "Location",
        "TechnicalArtifact",
        "KnowledgeArtifact",
        "MethodOrConcept",
        "DomainOrTopic",
        "Phenomenon",
        "QuantityMeasure",
        "Event",
    }
    for entity_type in v2_types:
        assert entity_type in description
    retired = {"Technology", "Topic", "Concept"}
    for retired_type in retired:
        assert re.search(rf"\b{retired_type}\b", description) is None


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


@pytest.mark.asyncio
async def test_search_memory_entity_path_threads_identity() -> None:
    """FRE-673: entity-match path threads ctx.user_id + ctx.authenticated into query_memory.

    The agent-invoked search_memory tool was the live path returning candidate_set_size=0:
    it called query_memory without identity, so the FRE-229 group-visibility filter dropped
    100% of the (all-'group') production memory.
    """
    from uuid import uuid4

    from personal_agent.telemetry.trace import TraceContext

    uid = uuid4()
    mock_service = MagicMock()
    mock_service.connected = True
    mock_service.query_memory = AsyncMock(return_value=MemoryQueryResult())

    ctx = TraceContext(trace_id="t-673", user_id=uid, authenticated=True)

    with patch.dict(sys.modules, {"personal_agent.service.app": _fake_app_module(mock_service)}):
        await search_memory_executor(query_text="Athens", entity_names=["Athens"], ctx=ctx)

    kwargs = mock_service.query_memory.call_args.kwargs
    assert kwargs.get("user_id") == uid
    assert kwargs.get("authenticated") is True


@pytest.mark.asyncio
async def test_search_memory_entity_path_threads_trace_and_session() -> None:
    """FRE-698: entity-match path threads ctx.trace_id + ctx.session_id into query_memory.

    These are the join keys the reranker fires inside query_memory needs (ADR-0074): the
    incident path previously passed trace_id but not session_id, so the joinability probe
    (which keys on session_id) could not attribute the rerank to its turn.
    """
    from personal_agent.telemetry.trace import TraceContext

    mock_service = MagicMock()
    mock_service.connected = True
    mock_service.query_memory = AsyncMock(return_value=MemoryQueryResult())

    ctx = TraceContext(trace_id="t-698", session_id="s-698")

    with patch.dict(sys.modules, {"personal_agent.service.app": _fake_app_module(mock_service)}):
        await search_memory_executor(query_text="Athens", entity_names=["Athens"], ctx=ctx)

    kwargs = mock_service.query_memory.call_args.kwargs
    assert kwargs.get("trace_id") == "t-698"
    assert kwargs.get("session_id") == "s-698"


@pytest.mark.asyncio
async def test_search_memory_broad_path_threads_identity() -> None:
    """FRE-673: broad-recall path threads ctx.user_id + ctx.authenticated into query_memory_broad."""
    from uuid import uuid4

    from personal_agent.telemetry.trace import TraceContext

    uid = uuid4()
    mock_service = MagicMock()
    mock_service.connected = True
    mock_service.query_memory_broad = AsyncMock(
        return_value={"entities": [], "sessions": [], "turns_summary": []}
    )

    ctx = TraceContext(trace_id="t-673", user_id=uid, authenticated=True)

    with patch.dict(sys.modules, {"personal_agent.service.app": _fake_app_module(mock_service)}):
        await search_memory_executor(query_text="what have I discussed before?", ctx=ctx)

    kwargs = mock_service.query_memory_broad.call_args.kwargs
    assert kwargs.get("user_id") == uid
    assert kwargs.get("authenticated") is True


@pytest.mark.asyncio
async def test_search_memory_explicit_window_sets_hard_recency() -> None:
    """FRE-658: an explicit positive recency_days is a HARD time window.

    The tool marks it via MemoryQuery.hard_recency_days so the de-gated recall
    paths re-apply a hard time bound (an explicit "last week about X" must not
    return all-time results once the relevance-bounded / multi-path flag is on).
    """
    mock_service = MagicMock()
    mock_service.connected = True
    mock_service.query_memory = AsyncMock(return_value=MemoryQueryResult())

    with patch.dict(sys.modules, {"personal_agent.service.app": _fake_app_module(mock_service)}):
        await search_memory_executor(
            query_text="last week about Athens", entity_names=["Athens"], recency_days=7
        )

    query = mock_service.query_memory.call_args.args[0]
    assert query.hard_recency_days == 7
    assert query.recency_days == 7


@pytest.mark.asyncio
async def test_search_memory_omitted_window_no_hard_recency() -> None:
    """FRE-658: an omitted recency_days is NOT a hard window (automatic-like).

    hard_recency_days stays None so the de-gated path remains invariant to the
    default recency_days (ADR-0100 AC-1a); the 90-day default is preserved as the
    soft/ranking window.
    """
    mock_service = MagicMock()
    mock_service.connected = True
    mock_service.query_memory = AsyncMock(return_value=MemoryQueryResult())

    with patch.dict(sys.modules, {"personal_agent.service.app": _fake_app_module(mock_service)}):
        await search_memory_executor(query_text="Athens", entity_names=["Athens"])

    query = mock_service.query_memory.call_args.args[0]
    assert query.hard_recency_days is None
    assert query.recency_days == 90


@pytest.mark.asyncio
async def test_search_memory_zero_window_no_hard_recency() -> None:
    """FRE-658: explicit 0 is not a hard window (0 is coerced to the 90 default).

    Documents the pre-existing "use 0 to search all history" docstring/behaviour
    mismatch (int(recency_days or 90) coerces 0 -> 90) — filed as a follow-up, not
    changed here. What FRE-658 asserts is only that 0 sets no hard window.
    """
    mock_service = MagicMock()
    mock_service.connected = True
    mock_service.query_memory = AsyncMock(return_value=MemoryQueryResult())

    with patch.dict(sys.modules, {"personal_agent.service.app": _fake_app_module(mock_service)}):
        await search_memory_executor(query_text="Athens", entity_names=["Athens"], recency_days=0)

    query = mock_service.query_memory.call_args.args[0]
    assert query.hard_recency_days is None
    assert query.recency_days == 90
