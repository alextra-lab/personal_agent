"""Unit tests for FreshnessConsumer (FRE-164 / ADR-0042 Step 4)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.events.consumers.freshness_consumer import FreshnessConsumer
from personal_agent.events.models import (
    AccessContext,
    ConsolidationCompletedEvent,
    MemoryAccessedEvent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    entity_ids: list[str],
    access_context: AccessContext = AccessContext.SEARCH,
    created_at: datetime | None = None,
    relationship_ids: list[str] | None = None,
) -> MemoryAccessedEvent:
    """Create a MemoryAccessedEvent with minimal required fields."""
    return MemoryAccessedEvent(
        entity_ids=entity_ids,
        relationship_ids=list(relationship_ids or []),
        access_context=access_context,
        query_type="test_query",
        trace_id="test-trace",
        session_id=None,
        created_at=created_at or datetime.now(timezone.utc),
        source_component="test",
    )


def _make_mock_driver(updated_count: int = 1) -> MagicMock:
    """Build a mock Neo4j async driver that returns a single-record result."""
    record = {"updated": updated_count}

    mock_result = AsyncMock()
    mock_result.single = AsyncMock(return_value=record)

    mock_db_session = AsyncMock()
    mock_db_session.run = AsyncMock(return_value=mock_result)
    mock_db_session.__aenter__ = AsyncMock(return_value=mock_db_session)
    mock_db_session.__aexit__ = AsyncMock(return_value=None)

    mock_driver = MagicMock()
    mock_driver.session = MagicMock(return_value=mock_db_session)
    return mock_driver


# ---------------------------------------------------------------------------
# Buffering and early-flush
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_ignores_non_memory_accessed_events() -> None:
    """Non-MemoryAccessedEvent types are silently dropped."""
    consumer = FreshnessConsumer(driver=_make_mock_driver(), batch_max_events=10)
    other_event = ConsolidationCompletedEvent(
        captures_processed=1,
        entities_created=0,
        entities_promoted=0,
        source_component="test",
    )
    await consumer.handle(other_event)
    assert len(consumer._buffer) == 0


@pytest.mark.asyncio
async def test_handle_buffers_memory_accessed_events() -> None:
    """MemoryAccessedEvent is added to the internal buffer."""
    consumer = FreshnessConsumer(driver=_make_mock_driver(), batch_max_events=10)
    event = _make_event(["Entity/A"])
    await consumer.handle(event)
    assert len(consumer._buffer) == 1
    assert consumer._buffer[0] is event


@pytest.mark.asyncio
async def test_early_flush_triggered_at_batch_max() -> None:
    """Buffer flushes immediately when batch_max_events is reached."""
    driver = _make_mock_driver()
    consumer = FreshnessConsumer(driver=driver, batch_max_events=3)
    await consumer.start()

    try:
        for i in range(3):
            await consumer.handle(_make_event([f"Entity/{i}"]))

        # Give the flush a moment to complete
        await asyncio.sleep(0)

        # Buffer should be empty after flush
        assert len(consumer._buffer) == 0
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_stop_drains_remaining_buffer() -> None:
    """stop() flushes any events remaining in the buffer."""
    driver = _make_mock_driver()
    consumer = FreshnessConsumer(driver=driver, batch_max_events=50)
    await consumer.start()

    # Queue 2 events (below flush threshold)
    await consumer.handle(_make_event(["Entity/X"]))
    await consumer.handle(_make_event(["Entity/Y"]))
    assert len(consumer._buffer) == 2

    await consumer.stop()
    # Buffer should be drained
    assert len(consumer._buffer) == 0


# ---------------------------------------------------------------------------
# Deduplication within a batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_batch_deduplicates_same_entity() -> None:
    """Multiple events for the same entity collapse into one Neo4j update."""
    driver = _make_mock_driver()
    consumer = FreshnessConsumer(driver=driver)

    t1 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2024, 1, 1, 12, 0, 5, tzinfo=timezone.utc)

    e1 = _make_event(["Entity/Alpha"], created_at=t1)
    e2 = _make_event(["Entity/Alpha"], created_at=t2)

    await consumer._write_batch([e1, e2])

    # Exactly one Cypher run call
    db_session = driver.session.return_value.__aenter__.return_value
    db_session.run.assert_called_once()

    call_args = db_session.run.call_args
    updates = call_args.kwargs["updates"] if call_args.kwargs else call_args.args[1]
    assert len(updates) == 1
    update = updates[0]
    assert update["entity_id"] == "Entity/Alpha"
    assert update["access_increment"] == 2  # two hits, one record
    # latest timestamp wins
    assert "2024-01-01T12:00:05" in update["last_accessed_at"]


@pytest.mark.asyncio
async def test_write_batch_latest_context_wins() -> None:
    """access_context reflects the event with the most recent created_at."""
    driver = _make_mock_driver()
    consumer = FreshnessConsumer(driver=driver)

    t_early = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    t_late = datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)

    e1 = _make_event(["Entity/Beta"], AccessContext.SEARCH, created_at=t_early)
    e2 = _make_event(["Entity/Beta"], AccessContext.TOOL_CALL, created_at=t_late)

    await consumer._write_batch([e1, e2])

    db_session = driver.session.return_value.__aenter__.return_value
    call_args = db_session.run.call_args
    updates = call_args.kwargs["updates"] if call_args.kwargs else call_args.args[1]
    assert updates[0]["access_context"] == AccessContext.TOOL_CALL.value


@pytest.mark.asyncio
async def test_write_batch_multiple_entities() -> None:
    """Events for different entities produce separate update records."""
    driver = _make_mock_driver(updated_count=2)
    consumer = FreshnessConsumer(driver=driver)

    e1 = _make_event(["Entity/A"])
    e2 = _make_event(["Entity/B"])
    e3 = _make_event(["Entity/A"])  # second hit on A

    await consumer._write_batch([e1, e2, e3])

    db_session = driver.session.return_value.__aenter__.return_value
    call_args = db_session.run.call_args_list[0]
    updates = call_args.kwargs["updates"] if call_args.kwargs else call_args.args[1]
    entity_map = {u["entity_id"]: u for u in updates}

    assert set(entity_map.keys()) == {"Entity/A", "Entity/B"}
    assert entity_map["Entity/A"]["access_increment"] == 2
    assert entity_map["Entity/B"]["access_increment"] == 1


@pytest.mark.asyncio
async def test_write_batch_updates_relationships() -> None:
    """Relationship elementIds get a separate UNWIND batch (ADR-0042)."""
    driver = _make_mock_driver(updated_count=1)
    consumer = FreshnessConsumer(driver=driver)

    r1 = _make_event([], relationship_ids=["5:abc:123:456"])
    r2 = _make_event([], relationship_ids=["5:abc:123:456"])

    await consumer._write_batch([r1, r2])

    db_session = driver.session.return_value.__aenter__.return_value
    assert db_session.run.await_count == 1
    call_args = db_session.run.call_args
    cypher = call_args.args[0]
    assert "elementId(r)" in cypher
    updates = call_args.kwargs["updates"]
    assert len(updates) == 1
    assert updates[0]["rel_id"] == "5:abc:123:456"
    assert updates[0]["access_increment"] == 2


@pytest.mark.asyncio
async def test_write_batch_entities_and_relationships_two_queries() -> None:
    """Batch with both kinds issues two Cypher executions."""
    driver = _make_mock_driver(updated_count=1)
    consumer = FreshnessConsumer(driver=driver)

    await consumer._write_batch(
        [
            _make_event(["E1"], relationship_ids=["5:r:1"]),
        ]
    )

    db_session = driver.session.return_value.__aenter__.return_value
    assert db_session.run.await_count == 2


@pytest.mark.asyncio
async def test_write_batch_skips_empty_entity_ids() -> None:
    """Entity IDs that are empty strings are filtered out."""
    driver = _make_mock_driver()
    consumer = FreshnessConsumer(driver=driver)

    event = _make_event(["", "Entity/Valid", ""])
    await consumer._write_batch([event])

    db_session = driver.session.return_value.__aenter__.return_value
    call_args = db_session.run.call_args
    updates = call_args.kwargs["updates"] if call_args.kwargs else call_args.args[1]
    assert len(updates) == 1
    assert updates[0]["entity_id"] == "Entity/Valid"


@pytest.mark.asyncio
async def test_write_batch_noop_when_all_empty() -> None:
    """_write_batch does not call Neo4j when all entity_ids are empty."""
    driver = _make_mock_driver()
    consumer = FreshnessConsumer(driver=driver)

    event = _make_event(["", ""])
    await consumer._write_batch([event])

    db_session = driver.session.return_value.__aenter__.return_value
    db_session.run.assert_not_called()


# ---------------------------------------------------------------------------
# Driver resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_driver_logs_warning_and_skips_neo4j(caplog: pytest.LogCaptureFixture) -> None:
    """When no driver is available, _write_batch logs and returns without error."""
    consumer = FreshnessConsumer(driver=None)

    # Prevent fallback from finding a global memory_service
    with patch(
        "personal_agent.events.consumers.freshness_consumer.FreshnessConsumer._resolve_driver",
        return_value=None,
    ):
        event = _make_event(["Entity/Z"])
        # Should not raise
        await consumer._write_batch([event])


@pytest.mark.asyncio
async def test_resolve_driver_uses_injected_driver() -> None:
    """_resolve_driver returns the injected driver when set."""
    mock_driver = MagicMock()
    consumer = FreshnessConsumer(driver=mock_driver)
    assert consumer._resolve_driver() is mock_driver


@pytest.mark.asyncio
async def test_resolve_driver_falls_back_to_memory_service() -> None:
    """_resolve_driver resolves from global memory_service when driver is None."""
    consumer = FreshnessConsumer(driver=None)
    mock_ms = MagicMock()
    mock_driver = MagicMock()
    mock_ms.driver = mock_driver

    with patch.dict(
        "sys.modules",
        {"personal_agent.service.app": MagicMock(memory_service=mock_ms)},
    ):
        resolved = consumer._resolve_driver()
        assert resolved is mock_driver


# ---------------------------------------------------------------------------
# Timer-based flush loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_loop_runs_and_is_cancelled_on_stop() -> None:
    """start() creates a background task that is cancelled by stop()."""
    consumer = FreshnessConsumer(driver=_make_mock_driver(), batch_window_seconds=60.0)
    await consumer.start()
    assert consumer._flush_task is not None
    assert not consumer._flush_task.done()

    await consumer.stop()
    assert consumer._flush_task is None
