"""Unit tests for query_elasticsearch native tool (ADR-0028 Phase 1).

Tests use mocked httpx responses — no Elasticsearch container required.
The executor returns dict[str, Any] on success and raises ToolExecutionError on failure.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.tools.elasticsearch import (
    query_elasticsearch_executor,
    query_elasticsearch_tool,
)
from personal_agent.tools.executor import ToolExecutionError


def _mock_response(status_code: int = 200, json_body: dict | None = None) -> MagicMock:
    """Build a mock httpx.Response."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = status_code
    mock_resp.is_error = status_code >= 400
    mock_resp.json.return_value = json_body or {}
    mock_resp.text = ""
    return mock_resp


def _mock_client(response: MagicMock) -> AsyncMock:
    """Build a mock AsyncClient returning the given response."""
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


# ── Tool definition tests ──────────────────────────────────────────────────


def test_tool_definition() -> None:
    """Tool has correct metadata."""
    assert query_elasticsearch_tool.name == "query_elasticsearch"
    assert query_elasticsearch_tool.category == "network"
    assert query_elasticsearch_tool.risk_level == "low"
    assert "NORMAL" in query_elasticsearch_tool.allowed_modes
    assert "ALERT" in query_elasticsearch_tool.allowed_modes
    assert "DEGRADED" in query_elasticsearch_tool.allowed_modes
    assert "LOCKDOWN" not in query_elasticsearch_tool.allowed_modes
    param_names = {p.name for p in query_elasticsearch_tool.parameters}
    assert {"action", "query", "index", "limit"} <= param_names


# ── Validation tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_action_raises() -> None:
    """Invalid action raises ToolExecutionError immediately."""
    with pytest.raises(ToolExecutionError, match="Invalid action"):
        await query_elasticsearch_executor(action="unknown_action")


@pytest.mark.asyncio
async def test_esql_missing_query_raises() -> None:
    """esql action without query raises ToolExecutionError."""
    esql_resp = _mock_response(200, {"columns": [], "values": []})
    with patch("personal_agent.tools.elasticsearch.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client(esql_resp)
        with pytest.raises(ToolExecutionError, match="'query' parameter is required"):
            await query_elasticsearch_executor(action="esql", query="")


@pytest.mark.asyncio
async def test_get_mappings_missing_index_raises() -> None:
    """get_mappings without index raises ToolExecutionError."""
    resp = _mock_response(200, {})
    with patch("personal_agent.tools.elasticsearch.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client(resp)
        with pytest.raises(ToolExecutionError, match="'index' parameter is required"):
            await query_elasticsearch_executor(action="get_mappings", index="")


# ── Success path tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_esql_success() -> None:
    """esql action returns columns and values."""
    body = {
        "columns": [{"name": "level"}, {"name": "message"}],
        "values": [["ERROR", "Something went wrong"]],
    }
    resp = _mock_response(200, body)
    with patch("personal_agent.tools.elasticsearch.httpx.AsyncClient") as mock_cls:
        mock_client = _mock_client(resp)
        mock_cls.return_value = mock_client
        result = await query_elasticsearch_executor(
            action="esql",
            query="FROM agent-logs-* | WHERE level='ERROR'",
        )

        # format must be a query param, not a body field (ES|QL /_query rejects body format)
        call_kwargs = mock_client.post.call_args.kwargs
        assert call_kwargs.get("params") == {"format": "json"}
        assert "format" not in call_kwargs.get("json", {})

    assert result["columns"] == ["level", "message"]
    assert result["row_count"] == 1
    assert "LIMIT 100" in result["query_used"]


@pytest.mark.asyncio
async def test_esql_respects_existing_limit() -> None:
    """esql does not inject LIMIT if query already contains one."""
    body = {"columns": [], "values": []}
    resp = _mock_response(200, body)
    with patch("personal_agent.tools.elasticsearch.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client(resp)
        result = await query_elasticsearch_executor(
            action="esql",
            query="FROM logs | LIMIT 5",
        )

    assert result["query_used"] == "FROM logs | LIMIT 5"


@pytest.mark.asyncio
async def test_list_indices_success() -> None:
    """list_indices returns indices list."""
    body = [{"index": "agent-logs-2026.04.01", "status": "open"}]
    resp = _mock_response(200, body)
    with patch("personal_agent.tools.elasticsearch.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client(resp)
        result = await query_elasticsearch_executor(action="list_indices")

    assert result["index_count"] == 1
    assert result["indices"][0]["index"] == "agent-logs-2026.04.01"


@pytest.mark.asyncio
async def test_get_mappings_success() -> None:
    """get_mappings returns mappings for a single index."""
    body = {
        "agent-logs-2026.04.01": {
            "mappings": {"properties": {"level": {"type": "keyword"}}}
        }
    }
    resp = _mock_response(200, body)
    with patch("personal_agent.tools.elasticsearch.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client(resp)
        result = await query_elasticsearch_executor(
            action="get_mappings", index="agent-logs-2026.04.01"
        )

    assert result["index"] == "agent-logs-2026.04.01"
    assert "properties" in result["mappings"]


@pytest.mark.asyncio
async def test_get_shards_success() -> None:
    """get_shards returns shard list."""
    body = [{"index": "agent-logs-2026.04.01", "shard": "0", "state": "STARTED"}]
    resp = _mock_response(200, body)
    with patch("personal_agent.tools.elasticsearch.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client(resp)
        result = await query_elasticsearch_executor(action="get_shards")

    assert result["shard_count"] == 1


# ── Error path tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_error_raises() -> None:
    """Non-2xx HTTP response raises ToolExecutionError with status code."""
    resp = _mock_response(
        500,
        {"error": {"reason": "index_not_found_exception"}},
    )
    with patch("personal_agent.tools.elasticsearch.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client(resp)
        with pytest.raises(ToolExecutionError, match="HTTP 500"):
            await query_elasticsearch_executor(
                action="esql", query="FROM missing-index | LIMIT 1"
            )


@pytest.mark.asyncio
async def test_connect_error_raises() -> None:
    """Connection failure raises ToolExecutionError."""
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    with patch("personal_agent.tools.elasticsearch.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = client
        with pytest.raises(ToolExecutionError, match="Cannot connect"):
            await query_elasticsearch_executor(action="esql", query="FROM logs | LIMIT 1")


@pytest.mark.asyncio
async def test_timeout_raises() -> None:
    """Timeout raises ToolExecutionError."""
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    with patch("personal_agent.tools.elasticsearch.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = client
        with pytest.raises(ToolExecutionError, match="timed out"):
            await query_elasticsearch_executor(action="esql", query="FROM logs | LIMIT 1")
