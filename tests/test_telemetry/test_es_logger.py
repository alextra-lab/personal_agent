"""Tests for ElasticsearchLogger request trace indexing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from personal_agent.telemetry.es_logger import ElasticsearchLogger


@pytest.mark.asyncio
async def test_index_request_trace_from_snapshot_writes_summary_and_steps() -> None:
    """Snapshot path indexes summary doc and per-step docs like index_request_trace."""
    logger = ElasticsearchLogger()
    mock_client = AsyncMock()
    mock_client.index = AsyncMock(
        side_effect=[
            {"_id": "trace_abc"},
            {"_id": "trace_abc_step_1"},
        ]
    )
    logger.client = mock_client

    summary = {
        "total_duration_ms": 12.5,
        "total_steps": 1,
        "phases_summary": {"setup": {"duration_ms": 1.0, "steps": 1}},
    }
    breakdown = [
        {
            "name": "session_db_lookup",
            "sequence": 1,
            "phase": "setup",
            "offset_ms": 0.0,
            "duration_ms": 1.0,
        },
        {"phase": "total", "offset_ms": 0.0, "duration_ms": 12.5},
    ]

    doc_id = await logger.index_request_trace_from_snapshot(
        trace_id="abc",
        trace_summary=summary,
        trace_breakdown=breakdown,
        session_id="sess-1",
    )

    assert doc_id == "trace_abc"
    assert mock_client.index.call_count == 2
    first_kw = mock_client.index.call_args_list[0].kwargs
    assert first_kw["id"] == "trace_abc"
    assert first_kw["document"]["trace_id"] == "abc"
    assert first_kw["document"]["session_id"] == "sess-1"
