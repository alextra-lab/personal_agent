"""Unit tests for the SLM-health ES sink (FRE-399 / ADR-0083)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_snapshot(status: str = "up") -> "SlmHealthSnapshot":
    from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot

    return SlmHealthSnapshot(
        status=status,  # type: ignore[arg-type]
        reachable=status != "down",
        probed_at=datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc),
        trace_id="test-trace-sink",
    )


class TestIndexNameFor:
    """index_name_for computes the correct daily index."""

    def test_format_is_prefix_plus_date(self) -> None:
        from personal_agent.observability.slm_health.sink import index_name_for

        snap = _make_snapshot()
        name = index_name_for(snap, prefix="agent-monitors-slm-health")
        assert name == "agent-monitors-slm-health-2026.06.02"

    def test_different_prefix(self) -> None:
        from personal_agent.observability.slm_health.sink import index_name_for

        snap = _make_snapshot()
        name = index_name_for(snap, prefix="my-custom-prefix")
        assert name.startswith("my-custom-prefix-")


class TestWriteResult:
    """write_result persists the snapshot to ES with a UUID doc id."""

    @pytest.mark.asyncio
    async def test_calls_es_index_with_correct_index(self) -> None:
        from personal_agent.observability.slm_health.sink import write_result

        es = AsyncMock()
        snap = _make_snapshot()
        await write_result(es, snap, prefix="agent-monitors-slm-health")

        es.index.assert_awaited_once()
        call_kwargs = es.index.call_args.kwargs
        assert call_kwargs["index"] == "agent-monitors-slm-health-2026.06.02"

    @pytest.mark.asyncio
    async def test_doc_id_is_a_uuid_string(self) -> None:
        import re

        from personal_agent.observability.slm_health.sink import write_result

        es = AsyncMock()
        snap = _make_snapshot()
        await write_result(es, snap, prefix="agent-monitors-slm-health")

        call_kwargs = es.index.call_args.kwargs
        doc_id = call_kwargs["id"]
        assert re.match(r"[0-9a-f-]{36}", doc_id), f"Not a UUID: {doc_id}"

    @pytest.mark.asyncio
    async def test_document_is_model_dump(self) -> None:
        from personal_agent.observability.slm_health.sink import write_result

        es = AsyncMock()
        snap = _make_snapshot(status="degraded")
        await write_result(es, snap, prefix="agent-monitors-slm-health")

        call_kwargs = es.index.call_args.kwargs
        doc = call_kwargs["document"]
        assert doc["status"] == "degraded"
        assert doc["kind"] == "system:slm_health_probe"
        # probed_at should be serialisable (ISO string from model_dump mode="json")
        assert isinstance(doc["probed_at"], str)
