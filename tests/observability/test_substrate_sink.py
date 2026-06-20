"""Unit tests for the flat per-substrate ES sink (FRE-550 / ADR-0074)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from personal_agent.observability.joinability.result import SubstrateResultDoc
from personal_agent.observability.joinability.sink import (
    substrate_index_name_for,
    write_substrate_results,
)

NOW = datetime(2026, 6, 20, 14, 0, 0, tzinfo=timezone.utc)


def _doc(substrate: str, run_id: str = "run-1") -> SubstrateResultDoc:
    return SubstrateResultDoc(
        run_id=run_id,
        started_at=NOW,
        substrate=substrate,
        status="green",
        expected="conditional",
        observed_count=1,
        duration_ms=1.0,
    )


def test_substrate_index_name_for() -> None:
    """Index name is ``{prefix}-substrate-YYYY.MM.DD`` from started_at."""
    name = substrate_index_name_for(_doc("postgres.sessions"), prefix="agent-monitors-joinability")
    assert name == "agent-monitors-joinability-substrate-2026.06.20"


@pytest.mark.asyncio
async def test_write_substrate_results_indexes_each_doc() -> None:
    """One es.index() per doc, with the right index, id, and JSON body."""
    es = AsyncMock()
    docs = [_doc("postgres.sessions"), _doc("elasticsearch.agent_logs")]
    await write_substrate_results(es, docs, prefix="agent-monitors-joinability", trace_id="trace-1")

    assert es.index.await_count == 2
    first = es.index.await_args_list[0].kwargs
    assert first["index"] == "agent-monitors-joinability-substrate-2026.06.20"
    assert first["id"] == "run-1::postgres.sessions"
    assert first["document"]["substrate"] == "postgres.sessions"
    # model_dump(mode="json") => started_at is an ISO string, not a datetime.
    assert isinstance(first["document"]["started_at"], str)

    second = es.index.await_args_list[1].kwargs
    assert second["id"] == "run-1::elasticsearch.agent_logs"


@pytest.mark.asyncio
async def test_write_substrate_results_empty_noop() -> None:
    """Empty docs list makes no es.index() calls."""
    es = AsyncMock()
    await write_substrate_results(es, [], prefix="agent-monitors-joinability", trace_id="t")
    es.index.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_substrate_results_error_propagates() -> None:
    """An es.index() failure propagates (the caller swallows, not the sink)."""
    es = AsyncMock()
    es.index.side_effect = RuntimeError("es down")
    with pytest.raises(RuntimeError, match="es down"):
        await write_substrate_results(
            es, [_doc("postgres.sessions")], prefix="agent-monitors-joinability", trace_id="t"
        )
