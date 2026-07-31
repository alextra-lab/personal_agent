"""Unit tests for the joinability run-doc ES sink (ADR-0074 Phase 5 / FRE-376)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from personal_agent.observability.joinability.result import ResultDoc
from personal_agent.observability.joinability.sink import index_name_for, write_result

NOW = datetime(2026, 6, 20, 14, 0, 0, tzinfo=timezone.utc)


def _doc(run_id: str = "run-1") -> ResultDoc:
    return ResultDoc(
        run_id=run_id,
        started_at=NOW,
        duration_ms=1.0,
        source="scheduler",
        window_hours=24,
        random_seed=1,
        sampled_session_id="session-1",
        sampled_trace_ids=["trace-1"],
        outcome="green",
        trace_id="trace-1",
        kind="run",
        substrate_checks=[],
        orphans=[],
    )


def test_index_name_for() -> None:
    """Index name is ``{prefix}-YYYY-MM`` from started_at (FRE-1036, monthly)."""
    name = index_name_for(_doc(), prefix="agent-monitors-joinability")
    assert name == "agent-monitors-joinability-2026-06"


@pytest.mark.asyncio
async def test_write_result_indexes_with_monthly_index_and_run_id_doc_id() -> None:
    """write_result calls es.index with the monthly index name and run_id doc id."""
    es = AsyncMock()
    doc = _doc()
    await write_result(es, doc, prefix="agent-monitors-joinability")

    es.index.assert_awaited_once()
    call_kwargs = es.index.call_args.kwargs
    assert call_kwargs["index"] == "agent-monitors-joinability-2026-06"
    assert call_kwargs["id"] == "run-1"
