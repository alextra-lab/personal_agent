"""Tests for ElasticsearchLogger update-by-query helpers.

ADR-0129 D3 / FRE-1067 retired ``index_request_trace``/
``index_request_trace_from_snapshot`` along with ``RequestTimer`` — the
request_trace/request_trace_step indexing tests that used to live here are
gone with the method.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.config.env_loader import Environment
from personal_agent.exceptions import VocabularyViolationError
from personal_agent.telemetry import vocabulary
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


# ---------------------------------------------------------------------------
# FRE-1178 AC-1: production mode stores a violating record unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_event_in_production_indexes_a_violation_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A violating record in production reaches ``client.index`` with the key intact.

    No exception propagates, the record is not dropped, and the offending
    key survives with its value intact — sanitising it would make the
    corpus look clean while destroying the evidence the counter exists to
    record (ADR-0133 D4, FRE-1178). Scoped to *the validator's own*
    behaviour: ``_index_agent_log`` still runs every document through
    ``redact_mapping`` (FRE-1068), a separate, pre-existing security control
    that can rewrite a secret-*shaped* value regardless of vocabulary
    status — this test uses a non-secret-shaped value (``duration_ms=12``)
    so redaction is not a confound.
    """
    mock_settings = MagicMock()
    mock_settings.environment = Environment.PRODUCTION
    monkeypatch.setattr(vocabulary, "settings", mock_settings)

    logger = ElasticsearchLogger()
    mock_client = AsyncMock()
    mock_client.index = AsyncMock(return_value={"_id": "doc-1"})
    logger.client = mock_client

    doc_id = await logger.log_event("task_started", {"duration_ms": 12})  # retired spelling

    assert doc_id == "doc-1"
    written = mock_client.index.call_args.kwargs["document"]
    assert written["duration_ms"] == 12
    assert written["event_type"] == "task_started"
