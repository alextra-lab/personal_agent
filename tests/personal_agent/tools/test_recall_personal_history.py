"""Unit tests for recall_personal_history (FRE-343)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.personal_history import recall_personal_history_executor


def _ctx(user_id):
    return SimpleNamespace(trace_id="trace-1", user_id=user_id)


def _mock_memory_service(records: list[dict]) -> MagicMock:
    """Build a connected MemoryService whose driver yields fixed records."""
    svc = MagicMock()
    svc.connected = True
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    result = AsyncMock()
    result.data = AsyncMock(return_value=records)
    mock_session.run = AsyncMock(return_value=result)

    svc.driver = MagicMock()
    svc.driver.session = lambda: mock_session
    return svc


@pytest.mark.asyncio
async def test_missing_user_id_raises(monkeypatch) -> None:
    """ctx.user_id=None raises ToolExecutionError with the 'bug' marker."""
    with pytest.raises(ToolExecutionError, match="missing_user_id"):
        await recall_personal_history_executor(days_ago=7, ctx=_ctx(None))


@pytest.mark.asyncio
async def test_returns_user_scoped_turns(monkeypatch) -> None:
    """Happy path — turns returned by the driver are surfaced unchanged."""
    uid = uuid4()
    now = datetime.now(timezone.utc)
    records = [
        {
            "turn_id": "t1",
            "timestamp": (now - timedelta(days=2)).isoformat(),
            "session_id": "s1",
            "user_message": "Athens trip planning",
            "summary": "discussed Athens itinerary",
            "entities": ["Athens", "Acropolis"],
        },
    ]
    svc = _mock_memory_service(records)
    monkeypatch.setattr("personal_agent.tools.personal_history._get_memory_service", lambda: svc)

    out = await recall_personal_history_executor(days_ago=7, ctx=_ctx(uid))

    assert out["total"] == 1
    assert out["window_days"] == 7
    assert out["user_id"] == str(uid)
    assert out["turns"][0]["turn_id"] == "t1"
    assert out["turns"][0]["entities"] == ["Athens", "Acropolis"]


@pytest.mark.asyncio
async def test_days_ago_out_of_range_raises(monkeypatch) -> None:
    """days_ago must be 1..365."""
    uid = uuid4()
    with pytest.raises(ToolExecutionError, match="days_ago"):
        await recall_personal_history_executor(days_ago=0, ctx=_ctx(uid))
    with pytest.raises(ToolExecutionError, match="days_ago"):
        await recall_personal_history_executor(days_ago=400, ctx=_ctx(uid))


@pytest.mark.asyncio
async def test_limit_clamped_to_1_50(monkeypatch) -> None:
    """limit is clamped, not rejected."""
    uid = uuid4()
    svc = _mock_memory_service([])
    monkeypatch.setattr("personal_agent.tools.personal_history._get_memory_service", lambda: svc)

    await recall_personal_history_executor(days_ago=7, limit=999, ctx=_ctx(uid))
    called_kwargs = svc.driver.session().__aenter__.return_value.run.call_args.kwargs  # type: ignore[union-attr]
    assert called_kwargs.get("limit") == 50

    await recall_personal_history_executor(days_ago=7, limit=0, ctx=_ctx(uid))
    called_kwargs = svc.driver.session().__aenter__.return_value.run.call_args.kwargs  # type: ignore[union-attr]
    assert called_kwargs.get("limit") == 1


@pytest.mark.asyncio
async def test_cypher_contains_topic_filter_when_set(monkeypatch) -> None:
    """topic substring appears as a Cypher parameter."""
    uid = uuid4()
    svc = _mock_memory_service([])
    monkeypatch.setattr("personal_agent.tools.personal_history._get_memory_service", lambda: svc)

    await recall_personal_history_executor(days_ago=7, topic="Athens", ctx=_ctx(uid))

    called_kwargs = svc.driver.session().__aenter__.return_value.run.call_args.kwargs  # type: ignore[union-attr]
    assert called_kwargs.get("topic") == "Athens"
    assert called_kwargs.get("user_id") == str(uid)


@pytest.mark.asyncio
async def test_topic_none_passes_null(monkeypatch) -> None:
    """When topic is unset, the Cypher parameter is None (drives WHERE branch)."""
    uid = uuid4()
    svc = _mock_memory_service([])
    monkeypatch.setattr("personal_agent.tools.personal_history._get_memory_service", lambda: svc)

    await recall_personal_history_executor(days_ago=7, ctx=_ctx(uid))

    called_kwargs = svc.driver.session().__aenter__.return_value.run.call_args.kwargs  # type: ignore[union-attr]
    assert called_kwargs.get("topic") is None
