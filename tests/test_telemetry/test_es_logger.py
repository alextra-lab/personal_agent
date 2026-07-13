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


@pytest.mark.asyncio
async def test_index_request_trace_from_snapshot_includes_user_id() -> None:
    """Verify user_id is written to both docs.

    ADR-0107 AC-3b: the hand-rolled request_trace/request_trace_step docs must
    carry user_id independently of structlog.contextvars, since this path
    builds its documents as dicts and bypasses structlog entirely.
    """
    logger = ElasticsearchLogger()
    mock_client = AsyncMock()
    mock_client.index = AsyncMock(
        side_effect=[
            {"_id": "trace_abc"},
            {"_id": "trace_abc_step_1"},
        ]
    )
    logger.client = mock_client

    summary = {"total_duration_ms": 12.5, "total_steps": 1, "phases_summary": {}}
    breakdown = [
        {"name": "session_db_lookup", "sequence": 1, "phase": "setup", "duration_ms": 1.0},
        {"phase": "total", "offset_ms": 0.0, "duration_ms": 12.5},
    ]

    await logger.index_request_trace_from_snapshot(
        trace_id="abc",
        trace_summary=summary,
        trace_breakdown=breakdown,
        session_id="sess-1",
        user_id="634c1446-642c-4d2b-88a9-1e783c9fb2d2",
    )

    trace_kw = mock_client.index.call_args_list[0].kwargs
    step_kw = mock_client.index.call_args_list[1].kwargs
    assert trace_kw["document"]["user_id"] == "634c1446-642c-4d2b-88a9-1e783c9fb2d2"
    assert step_kw["document"]["user_id"] == "634c1446-642c-4d2b-88a9-1e783c9fb2d2"


@pytest.mark.asyncio
async def test_index_request_trace_from_snapshot_user_id_defaults_none() -> None:
    """Verify a missing user_id is written explicitly as None, not omitted.

    A downstream AC-3b query must reliably find it absent, not merely
    never-mapped.
    """
    logger = ElasticsearchLogger()
    mock_client = AsyncMock()
    mock_client.index = AsyncMock(return_value={"_id": "trace_abc"})
    logger.client = mock_client

    await logger.index_request_trace_from_snapshot(
        trace_id="abc",
        trace_summary={"total_duration_ms": 1.0, "total_steps": 0, "phases_summary": {}},
        trace_breakdown=[],
        session_id="sess-1",
    )

    trace_kw = mock_client.index.call_args_list[0].kwargs
    assert trace_kw["document"]["user_id"] is None


@pytest.mark.asyncio
async def test_update_by_query_calls_client_with_script() -> None:
    """update_by_query issues a Painless-script partial update scoped to the query."""
    logger = ElasticsearchLogger()
    mock_client = AsyncMock()
    mock_client.update_by_query = AsyncMock(return_value={"updated": 3})
    logger.client = mock_client

    updated = await logger.update_by_query(
        "agent-insights-*",
        {"term": {"fingerprint": "fp-abc"}},
        "ctx._source.linear_issue_id = params.linear_issue_id",
        {"linear_issue_id": "FRE-999"},
    )

    assert updated == 3
    kwargs = mock_client.update_by_query.call_args.kwargs
    assert kwargs["index"] == "agent-insights-*"
    assert kwargs["query"] == {"term": {"fingerprint": "fp-abc"}}
    assert kwargs["script"]["source"] == "ctx._source.linear_issue_id = params.linear_issue_id"
    assert kwargs["script"]["params"] == {"linear_issue_id": "FRE-999"}


@pytest.mark.asyncio
async def test_update_by_query_returns_zero_when_not_connected() -> None:
    """No client configured -> returns 0 without raising (best-effort, mirrors index_document)."""
    logger = ElasticsearchLogger()
    assert logger.client is None

    updated = await logger.update_by_query(
        "agent-insights-*",
        {"term": {"fingerprint": "fp-abc"}},
        "ctx._source.x = params.x",
        {"x": 1},
    )

    assert updated == 0


@pytest.mark.asyncio
async def test_update_by_query_swallows_client_errors() -> None:
    """A client exception is logged and swallowed, returning 0 (best-effort, never raises)."""
    logger = ElasticsearchLogger()
    mock_client = AsyncMock()
    mock_client.update_by_query = AsyncMock(side_effect=RuntimeError("es down"))
    logger.client = mock_client

    updated = await logger.update_by_query(
        "agent-insights-*",
        {"term": {"fingerprint": "fp-abc"}},
        "ctx._source.x = params.x",
        {"x": 1},
    )

    assert updated == 0
