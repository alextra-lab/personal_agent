"""Tests for Captain's Log Elasticsearch indexer (Phase 2.3)."""

import asyncio
from typing import Any

import pytest

from personal_agent.captains_log.es_indexer import (
    build_es_indexer_from_handler,
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
        async def indexer(
            index_name: str, document: dict, doc_id: str | None = None
        ) -> None:
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
        called: list[tuple[str, dict, str | None]] = []

        async def indexer(
            index_name: str, document: dict, doc_id: str | None = None
        ) -> None:
            called.append((index_name, document, doc_id))

        set_es_indexer(indexer)
        try:
            schedule_es_index(
                "agent-captains-captures-2026-02-22",
                {"trace_id": "t1"},
                doc_id="trace-123",
            )
            # Allow the created task to run
            await asyncio.sleep(0.05)
            assert len(called) == 1
            assert called[0][0] == "agent-captains-captures-2026-02-22"
            assert called[0][1] == {"trace_id": "t1"}
            assert called[0][2] == "trace-123"
        finally:
            set_es_indexer(None)

    @pytest.mark.asyncio
    async def test_schedule_es_index_swallows_indexer_exception(self) -> None:
        """When indexer raises, schedule_es_index does not propagate (non-blocking)."""
        async def indexer(
            index_name: str, document: dict, doc_id: str | None = None
        ) -> None:
            raise RuntimeError("ES unavailable")

        set_es_indexer(indexer)
        try:
            schedule_es_index("test-index", {"x": 1})
            await asyncio.sleep(0.05)
            # No exception should propagate
        finally:
            set_es_indexer(None)


class TestBuildESIndexerFromHandler:
    """Test build_es_indexer_from_handler and doc_id passthrough (FRE-30)."""

    @pytest.mark.asyncio
    async def test_build_indexer_returns_none_when_handler_disconnected(self) -> None:
        """When handler is not connected, build returns None."""
        handler = type("H", (), {"_connected": False, "es_logger": None})()
        assert build_es_indexer_from_handler(handler) is None

    @pytest.mark.asyncio
    async def test_build_indexer_passes_doc_id_to_es_logger(self) -> None:
        """Built indexer calls es_logger.index_document with id=doc_id."""
        seen: list[tuple[str, dict, str | None]] = []

        async def index_doc(
            self: Any,
            index_name: str,
            document: dict,
            *,
            id: str | None = None,
        ) -> str | None:
            seen.append((index_name, document, id))
            return id or "auto"

        class Handler:
            _connected = True
            es_logger = type("Logger", (), {"index_document": index_doc})()

        indexer = build_es_indexer_from_handler(Handler())
        assert indexer is not None
        await indexer("idx-1", {"a": 1}, doc_id="doc-123")
        assert len(seen) == 1
        assert seen[0][0] == "idx-1"
        assert seen[0][1] == {"a": 1}
        assert seen[0][2] == "doc-123"
