"""Unit tests for the delivery-ratio ES sink (FRE-1051 / FRE-1189)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest


def _make_doc(status: str = "pass") -> "DeliveryRatioResultDoc":
    from personal_agent.observability.delivery_ratio.result import (
        DeliveryRatioResultDoc,
        FamilyDeliveryRecord,
    )

    families = [
        FamilyDeliveryRecord(
            family="api_cost_recorded",
            oracle="postgres:api_costs",
            oracle_count=100,
            es_count=100 if status == "pass" else 40,
            ratio=1.0 if status == "pass" else 0.4,
            lost=0 if status == "pass" else 60,
            status=status,  # type: ignore[arg-type]
            min_ratio=0.99,
            zero_cause=None,
        ),
        FamilyDeliveryRecord(
            family="turn.model_call_completed",
            oracle=None,
            oracle_count=None,
            es_count=12,
            ratio=None,
            lost=None,
            status="unverifiable",
            min_ratio=0.99,
            zero_cause=None,
        ),
    ]
    return DeliveryRatioResultDoc(
        run_at=datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc),
        since=date(2026, 6, 1),
        until=date(2026, 6, 1),
        status=status if status != "pass" else "pass",  # type: ignore[arg-type]
        families=families,
        trace_id="test-trace-delivery-ratio",
    )


class TestIndexNameFor:
    """index_name_for computes the correct monthly index (FRE-543)."""

    def test_format_is_prefix_plus_month(self) -> None:
        from personal_agent.observability.delivery_ratio.sink import index_name_for

        doc = _make_doc()
        name = index_name_for(doc, prefix="agent-monitors-delivery-ratio")
        assert name == "agent-monitors-delivery-ratio-2026-06"

    def test_different_prefix(self) -> None:
        from personal_agent.observability.delivery_ratio.sink import index_name_for

        doc = _make_doc()
        name = index_name_for(doc, prefix="my-custom-prefix")
        assert name.startswith("my-custom-prefix-")


class TestWriteResult:
    """write_result persists the doc to ES, carrying the per-family verdict (AC-2)."""

    @pytest.mark.asyncio
    async def test_calls_es_index_with_correct_index(self) -> None:
        from personal_agent.observability.delivery_ratio.sink import write_result

        es = AsyncMock()
        doc = _make_doc()
        await write_result(es, doc, prefix="agent-monitors-delivery-ratio")

        es.index.assert_awaited_once()
        call_kwargs = es.index.call_args.kwargs
        assert call_kwargs["index"] == "agent-monitors-delivery-ratio-2026-06"

    @pytest.mark.asyncio
    async def test_doc_id_is_a_uuid_string(self) -> None:
        import re

        from personal_agent.observability.delivery_ratio.sink import write_result

        es = AsyncMock()
        doc = _make_doc()
        await write_result(es, doc, prefix="agent-monitors-delivery-ratio")

        call_kwargs = es.index.call_args.kwargs
        doc_id = call_kwargs["id"]
        assert re.match(r"[0-9a-f-]{36}", doc_id), f"Not a UUID: {doc_id}"

    @pytest.mark.asyncio
    async def test_document_carries_per_family_verdict_including_unverifiable(self) -> None:
        """AC-2: the written document carries the per-family verdict, explicitly including
        the unverifiable case — not merely that the probe ran.
        """
        from personal_agent.observability.delivery_ratio.sink import write_result

        es = AsyncMock()
        doc = _make_doc(status="breach")
        await write_result(es, doc, prefix="agent-monitors-delivery-ratio")

        call_kwargs = es.index.call_args.kwargs
        written = call_kwargs["document"]
        assert written["status"] == "breach"
        assert written["families"][0]["status"] == "breach"
        assert written["families"][0]["ratio"] == 0.4
        assert written["families"][1]["status"] == "unverifiable"
        assert written["families"][1]["oracle"] is None
        assert written["kind"] == "system:delivery_ratio_probe"
        assert isinstance(written["run_at"], str)
