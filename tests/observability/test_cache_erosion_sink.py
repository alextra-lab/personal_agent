"""Unit tests for the cache-erosion ES sink (ADR-0078 / FRE-1189)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest


def _make_doc(any_eroded: bool = False) -> "CacheErosionResultDoc":
    from personal_agent.observability.cache_erosion.result import (
        CacheErosionResultDoc,
        CallsiteErosionRecord,
    )

    return CacheErosionResultDoc(
        run_at=datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc),
        window_days=2,
        threshold=0.9,
        any_eroded=any_eroded,
        results=[
            CallsiteErosionRecord(
                callsite="orchestrator.primary",
                day_a=date(2026, 6, 1),
                day_b=date(2026, 6, 2),
                hash_count_a=1,
                hash_count_b=2 if any_eroded else 1,
                jaccard=0.5 if any_eroded else 1.0,
                status="eroded" if any_eroded else "stable",
                threshold=0.9,
            )
        ],
        trace_id="test-trace-cache-erosion",
    )


class TestIndexNameFor:
    """index_name_for computes the correct monthly index (FRE-543)."""

    def test_format_is_prefix_plus_month(self) -> None:
        from personal_agent.observability.cache_erosion.sink import index_name_for

        doc = _make_doc()
        name = index_name_for(doc, prefix="agent-monitors-cache-erosion")
        assert name == "agent-monitors-cache-erosion-2026-06"

    def test_different_prefix(self) -> None:
        from personal_agent.observability.cache_erosion.sink import index_name_for

        doc = _make_doc()
        name = index_name_for(doc, prefix="my-custom-prefix")
        assert name.startswith("my-custom-prefix-")


class TestWriteResult:
    """write_result persists the doc to ES with a UUID doc id and carries its verdict."""

    @pytest.mark.asyncio
    async def test_calls_es_index_with_correct_index(self) -> None:
        from personal_agent.observability.cache_erosion.sink import write_result

        es = AsyncMock()
        doc = _make_doc()
        await write_result(es, doc, prefix="agent-monitors-cache-erosion")

        es.index.assert_awaited_once()
        call_kwargs = es.index.call_args.kwargs
        assert call_kwargs["index"] == "agent-monitors-cache-erosion-2026-06"

    @pytest.mark.asyncio
    async def test_doc_id_is_a_uuid_string(self) -> None:
        import re

        from personal_agent.observability.cache_erosion.sink import write_result

        es = AsyncMock()
        doc = _make_doc()
        await write_result(es, doc, prefix="agent-monitors-cache-erosion")

        call_kwargs = es.index.call_args.kwargs
        doc_id = call_kwargs["id"]
        assert re.match(r"[0-9a-f-]{36}", doc_id), f"Not a UUID: {doc_id}"

    @pytest.mark.asyncio
    async def test_document_carries_the_verdict(self) -> None:
        """AC-2: the written document carries the computed verdict, not just that it ran."""
        from personal_agent.observability.cache_erosion.sink import write_result

        es = AsyncMock()
        doc = _make_doc(any_eroded=True)
        await write_result(es, doc, prefix="agent-monitors-cache-erosion")

        call_kwargs = es.index.call_args.kwargs
        written = call_kwargs["document"]
        assert written["any_eroded"] is True
        assert written["results"][0]["status"] == "eroded"
        assert written["results"][0]["jaccard"] == 0.5
        assert written["kind"] == "system:cache_erosion_probe"
        # run_at should be serialisable (ISO string from model_dump mode="json")
        assert isinstance(written["run_at"], str)
