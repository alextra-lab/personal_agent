"""Tests for ElasticsearchLogger update-by-query helpers.

ADR-0129 D3 / FRE-1067 retired ``index_request_trace``/
``index_request_trace_from_snapshot`` along with ``RequestTimer`` — the
request_trace/request_trace_step indexing tests that used to live here are
gone with the method.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from personal_agent.telemetry.es_logger import ElasticsearchLogger


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
