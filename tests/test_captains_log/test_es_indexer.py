"""Tests for Captain's Log Elasticsearch indexer (Phase 2.3)."""

import asyncio
from typing import Any

import pytest

from personal_agent.captains_log.es_indexer import (
    build_es_indexer_from_handler,
    get_es_indexer,
    normalize_capture_doc_for_es,
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

        async def indexer(index_name: str, document: dict, doc_id: str | None = None) -> None:
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

        async def indexer(index_name: str, document: dict, doc_id: str | None = None) -> None:
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

        async def indexer(index_name: str, document: dict, doc_id: str | None = None) -> None:
            raise RuntimeError("ES unavailable")

        set_es_indexer(indexer)
        try:
            schedule_es_index("test-index", {"x": 1})
            await asyncio.sleep(0.05)
            # No exception should propagate
        finally:
            set_es_indexer(None)

    @pytest.mark.asyncio
    async def test_schedule_es_index_normalizes_captures_tool_results_output(self) -> None:
        """Captures index gets tool_results[].output serialized: strings pass through, dicts become JSON strings."""
        called: list[tuple[str, dict, str | None]] = []

        async def indexer(index_name: str, document: dict, doc_id: str | None = None) -> None:
            called.append((index_name, document, doc_id))

        set_es_indexer(indexer)
        try:
            schedule_es_index(
                "agent-captains-captures-2026-02-22",
                {
                    "trace_id": "t1",
                    "tool_results": [
                        {"tool_name": "run", "success": True, "output": "stdout text", "error": None, "latency_ms": 10},
                        {"tool_name": "read", "success": True, "output": {"path": "/tmp/x", "content": "hi"}, "error": None, "latency_ms": 5},
                    ],
                },
                doc_id="trace-1",
            )
            await asyncio.sleep(0.05)
            assert len(called) == 1
            doc = called[0][1]
            assert doc["tool_results"][0]["output"] == "stdout text"
            assert doc["tool_results"][1]["output"] == '{"path": "/tmp/x", "content": "hi"}'
        finally:
            set_es_indexer(None)


class TestNormalizeCaptureDocForES:
    """Test normalize_capture_doc_for_es.

    ES maps tool_results[].output as ``text`` (index: false).
    Non-string outputs (dicts, lists, None) must be JSON-serialized to strings.
    """

    def test_passthrough_when_no_tool_results(self) -> None:
        """Doc without tool_results is returned unchanged."""
        doc = {"trace_id": "t1", "user_message": "hi"}
        assert normalize_capture_doc_for_es(doc) == doc

    def test_passthrough_when_tool_results_not_list(self) -> None:
        """Doc with non-list tool_results is returned unchanged."""
        doc = {"trace_id": "t1", "tool_results": "invalid"}
        assert normalize_capture_doc_for_es(doc) == doc

    def test_string_output_unchanged(self) -> None:
        """String output passes through unchanged — already a valid ES text value."""
        doc = {
            "trace_id": "t1",
            "tool_results": [
                {"tool_name": "run", "success": True, "output": "hello", "error": None},
            ],
        }
        out = normalize_capture_doc_for_es(doc)
        assert out["tool_results"][0]["output"] == "hello"
        assert doc["tool_results"][0]["output"] == "hello"  # input not mutated

    def test_serializes_dict_output(self) -> None:
        """Dict output is JSON-serialized to a string so ES text field accepts it."""
        doc = {
            "trace_id": "t1",
            "tool_results": [
                {"tool_name": "run", "success": True, "output": {"key": "val"}, "error": None},
            ],
        }
        out = normalize_capture_doc_for_es(doc)
        assert out["tool_results"][0]["output"] == '{"key": "val"}'
        assert doc["tool_results"][0]["output"] == {"key": "val"}  # input not mutated

    def test_serializes_non_string_output(self) -> None:
        """None, list, and number outputs are JSON-serialized to strings."""
        doc = {
            "trace_id": "t1",
            "tool_results": [
                {"tool_name": "a", "output": None},
                {"tool_name": "b", "output": [1, 2]},
                {"tool_name": "c", "output": 42},
            ],
        }
        out = normalize_capture_doc_for_es(doc)
        assert out["tool_results"][0]["output"] == "null"
        assert out["tool_results"][1]["output"] == "[1, 2]"
        assert out["tool_results"][2]["output"] == "42"

    def test_non_dict_item_wrapped_as_value(self) -> None:
        """Non-dict list item (malformed tool_result) is coerced to a string."""
        doc = {"trace_id": "t1", "tool_results": ["broken"]}
        out = normalize_capture_doc_for_es(doc)
        assert out["tool_results"] == [{"value": "broken"}]


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
