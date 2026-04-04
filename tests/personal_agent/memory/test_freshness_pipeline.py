"""Integration-style test: memory.accessed publish → FreshnessConsumer → Neo4j (FRE-166 / ADR-0042)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.events import AccessContext, MemoryAccessedEvent
from personal_agent.events.consumers.freshness_consumer import FreshnessConsumer
from personal_agent.memory.models import MemoryQuery
from personal_agent.memory.service import MemoryService


@pytest.mark.asyncio
async def test_query_memory_event_reaches_consumer_neo4j_batch() -> None:
    """query_memory publishes MemoryAccessedEvent; consumer flush issues Neo4j UNWIND update."""
    service = MemoryService()
    service.connected = True
    service.driver = MagicMock()

    mock_session = AsyncMock()
    mock_result = AsyncMock()
    mock_result.values = AsyncMock(return_value=[])
    mock_result.data = AsyncMock(return_value=[])
    mock_result.single = AsyncMock(return_value={"ids": []})
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    service.driver.session = MagicMock(return_value=mock_session)

    captured: list[MemoryAccessedEvent] = []

    async def capture_publish(_stream: str, event: object) -> None:
        if isinstance(event, MemoryAccessedEvent):
            captured.append(event)

    mock_bus = AsyncMock()
    mock_bus.publish = AsyncMock(side_effect=capture_publish)

    consumer_driver = MagicMock()
    fr_record = {"updated": 2}
    fr_result = AsyncMock()
    fr_result.single = AsyncMock(return_value=fr_record)
    fr_db = AsyncMock()
    fr_db.run = AsyncMock(return_value=fr_result)
    fr_db.__aenter__ = AsyncMock(return_value=fr_db)
    fr_db.__aexit__ = AsyncMock(return_value=None)
    consumer_driver.session = MagicMock(return_value=fr_db)

    query = MemoryQuery(entity_names=["Alpha", "Beta"], limit=5)

    with (
        patch("personal_agent.memory.service.settings") as mock_settings,
        patch("personal_agent.memory.service.get_event_bus", return_value=mock_bus),
    ):
        mock_settings.freshness_enabled = True
        mock_settings.reranker_enabled = False
        mock_settings.embedding_dimensions = 768

        await service.query_memory(
            query,
            trace_id="trace-pipe",
            session_id="sess-pipe",
            access_context=AccessContext.SEARCH,
        )

    assert len(captured) == 1
    assert captured[0].trace_id == "trace-pipe"
    assert set(captured[0].entity_ids) >= {"Alpha", "Beta"}

    consumer = FreshnessConsumer(driver=consumer_driver, batch_window_seconds=60.0, batch_max_events=50)
    await consumer.handle(captured[0])
    await consumer._flush()

    fr_db.run.assert_awaited()
    call_kw = fr_db.run.await_args
    assert call_kw is not None
    cypher = call_kw.args[0] if call_kw.args else ""
    updates = call_kw.kwargs.get("updates") if call_kw.kwargs else None
    if updates is None and len(call_kw.args) > 1:
        updates = call_kw.args[1]
    assert "UNWIND" in cypher
    assert isinstance(updates, list)
    assert len(updates) >= 1
    names = {u["entity_id"] for u in updates}
    assert names >= {"Alpha", "Beta"}
    for u in updates:
        if u["entity_id"] in ("Alpha", "Beta"):
            assert u["access_increment"] >= 1
            assert isinstance(u["last_accessed_at"], str)
