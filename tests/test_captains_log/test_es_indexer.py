"""Tests for Captain's Log Elasticsearch indexer (Phase 2.3)."""

import asyncio

import pytest

from personal_agent.captains_log.es_indexer import (
    get_es_indexer,
    schedule_es_index,
    set_es_indexer,
)


class TestESIndexerRegistry:
    """Test set/get ES indexer."""

    def test_get_es_indexer_none_by_default(self) -> None:
        """Without setting, get_es_indexer returns None."""
        set_es_indexer(None)
        assert get_es_indexer() is None

    def test_set_and_get_es_indexer(self) -> None:
        """set_es_indexer stores the callable; get_es_indexer returns it."""
        async def indexer(index_name: str, document: dict) -> None:
            pass

        set_es_indexer(indexer)
        assert get_es_indexer() is indexer
        set_es_indexer(None)
        assert get_es_indexer() is None


class TestScheduleESIndex:
    """Test schedule_es_index behavior."""

    def test_schedule_es_index_no_op_when_no_indexer(self) -> None:
        """When no indexer is set, schedule_es_index does nothing."""
        set_es_indexer(None)
        schedule_es_index("test-index", {"a": 1})  # should not raise

    @pytest.mark.asyncio
    async def test_schedule_es_index_calls_indexer_when_set(self) -> None:
        """When indexer is set, schedule_es_index invokes it (non-blocking)."""
        called: list[tuple[str, dict]] = []

        async def indexer(index_name: str, document: dict) -> None:
            called.append((index_name, document))

        set_es_indexer(indexer)
        try:
            schedule_es_index("agent-captains-captures-2026-02-22", {"trace_id": "t1"})
            # Allow the created task to run
            await asyncio.sleep(0.05)
            assert len(called) == 1
            assert called[0][0] == "agent-captains-captures-2026-02-22"
            assert called[0][1] == {"trace_id": "t1"}
        finally:
            set_es_indexer(None)

    @pytest.mark.asyncio
    async def test_schedule_es_index_swallows_indexer_exception(self) -> None:
        """When indexer raises, schedule_es_index does not propagate (non-blocking)."""
        async def indexer(index_name: str, document: dict) -> None:
            raise RuntimeError("ES unavailable")

        set_es_indexer(indexer)
        try:
            schedule_es_index("test-index", {"x": 1})
            await asyncio.sleep(0.05)
            # No exception should propagate
        finally:
            set_es_indexer(None)
