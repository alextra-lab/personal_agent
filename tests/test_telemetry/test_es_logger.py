"""Tests for ElasticsearchLogger update-by-query helpers.

ADR-0129 D3 / FRE-1067 retired ``index_request_trace``/
``index_request_trace_from_snapshot`` along with ``RequestTimer`` — the
request_trace/request_trace_step indexing tests that used to live here are
gone with the method.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from personal_agent.exceptions import VocabularyViolationError
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


# ---------------------------------------------------------------------------
# ADR-0133: the governed vocabulary validator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_event_rejects_a_retired_spelling_in_caller_data() -> None:
    """A retired spelling supplied through the caller's own ``data`` raises, not indexes."""
    logger = ElasticsearchLogger()
    mock_client = AsyncMock()
    logger.client = mock_client

    with pytest.raises(VocabularyViolationError):
        await logger.log_event("task_started", {"duration_ms": 12})

    mock_client.index.assert_not_called()


@pytest.mark.asyncio
async def test_log_event_validates_the_span_id_it_merges_itself() -> None:
    """ADR-0133 AC-6: a key ``log_event`` merges itself — not supplied via ``data`` — is checked.

    ``span_id`` here comes from the ``span_id`` parameter and never touches the
    caller's ``data`` dict, so a validator that only inspected ``data`` would
    miss it entirely.
    """
    logger = ElasticsearchLogger()
    mock_client = AsyncMock()
    logger.client = mock_client

    with pytest.raises(VocabularyViolationError) as exc_info:
        await logger.log_event("task_started", {}, span_id=12345)

    assert exc_info.value.field == "span_id"
    mock_client.index.assert_not_called()


@pytest.mark.asyncio
async def test_log_event_validates_the_timestamp_it_merges_itself() -> None:
    """ADR-0133 AC-6: ``@timestamp``, merged from the ``timestamp`` parameter, is checked too."""
    logger = ElasticsearchLogger()
    mock_client = AsyncMock()
    logger.client = mock_client

    with pytest.raises(VocabularyViolationError) as exc_info:
        await logger.log_event("task_started", {}, timestamp=1234567890)

    assert exc_info.value.field == "@timestamp"
    mock_client.index.assert_not_called()


@pytest.mark.asyncio
async def test_log_event_validates_the_event_type_it_merges_itself() -> None:
    """ADR-0133 AC-6: ``event_type``, merged from the positional argument, is checked too.

    ``trace_id`` is not separately tested here: ``log_event`` always coerces it
    to ``str(trace_id) if trace_id else None`` before merging, so it can never
    carry a wrong-typed value regardless of what the caller passes in.
    """
    logger = ElasticsearchLogger()
    mock_client = AsyncMock()
    logger.client = mock_client

    with pytest.raises(VocabularyViolationError) as exc_info:
        await logger.log_event(12345, {})  # event_type declared str

    assert exc_info.value.field == "event_type"
    mock_client.index.assert_not_called()
