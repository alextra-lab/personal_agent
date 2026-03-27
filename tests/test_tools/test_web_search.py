"""Unit tests for web_search native tool (ADR-0034).

Tests use mocked httpx responses — no SearXNG container required.
The executor returns dict[str, Any] on success and raises ToolExecutionError on failure.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.web import web_search_executor, web_search_tool


def _mock_searxng_response(
    results: list[dict] | None = None,
    suggestions: list[str] | None = None,
    infoboxes: list[dict] | None = None,
) -> MagicMock:
    """Build a mock httpx response with SearXNG JSON structure."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "results": results or [],
        "suggestions": suggestions or [],
        "infoboxes": infoboxes or [],
    }
    return mock_resp


def _mock_client(response: MagicMock) -> AsyncMock:
    """Build a mock AsyncClient that returns the given response on .get()."""
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


# ── Tool definition tests ──────────────────────────────────────────────────


def test_web_search_tool_definition() -> None:
    """Tool has correct metadata for governance and LLM awareness."""
    assert web_search_tool.name == "web_search"
    assert web_search_tool.category == "network"
    assert web_search_tool.risk_level == "low"
    assert "NORMAL" in web_search_tool.allowed_modes
    assert "ALERT" in web_search_tool.allowed_modes
    assert "DEGRADED" in web_search_tool.allowed_modes
    assert "LOCKDOWN" not in web_search_tool.allowed_modes
    assert "RECOVERY" not in web_search_tool.allowed_modes
    param_names = {p.name for p in web_search_tool.parameters}
    assert "query" in param_names
    assert "categories" in param_names
    assert "engines" in param_names
    assert "language" in param_names
    assert "time_range" in param_names
    assert "max_results" in param_names
    # query is the only required parameter
    required = [p for p in web_search_tool.parameters if p.required]
    assert len(required) == 1
    assert required[0].name == "query"


# ── Executor happy-path tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_search_happy_path() -> None:
    """Successful search returns structured dict with results."""
    resp = _mock_searxng_response(
        results=[
            {
                "title": "Python 3.12 docs",
                "url": "https://docs.python.org/3.12/",
                "content": "Official Python docs",
                "engine": "google",
                "score": 0.9,
            }
        ],
        suggestions=["python tutorial"],
    )
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="python docs")

    assert isinstance(result, dict)
    assert result["result_count"] == 1
    assert result["results"][0]["title"] == "Python 3.12 docs"
    assert result["results"][0]["url"] == "https://docs.python.org/3.12/"
    assert result["results"][0]["snippet"] == "Official Python docs"
    assert result["results"][0]["engine"] == "google"
    assert result["suggestions"] == ["python tutorial"]
    assert result["query"] == "python docs"


@pytest.mark.asyncio
async def test_web_search_empty_results() -> None:
    """Zero results still returns success dict with result_count=0."""
    resp = _mock_searxng_response(results=[])
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="xyzzy_nonexistent_query")

    assert isinstance(result, dict)
    assert result["result_count"] == 0
    assert result["results"] == []


@pytest.mark.asyncio
async def test_web_search_categories_passed_in_params() -> None:
    """categories parameter is forwarded to SearXNG query params."""
    resp = _mock_searxng_response()
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        await web_search_executor(query="asyncio", categories="it")

    call_kwargs = client.get.call_args
    params = call_kwargs.kwargs["params"]
    assert params["categories"] == "it"


@pytest.mark.asyncio
async def test_web_search_engines_passed_in_params() -> None:
    """engines parameter is forwarded to SearXNG query params."""
    resp = _mock_searxng_response()
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        await web_search_executor(query="test", engines="google,stackoverflow")

    call_kwargs = client.get.call_args
    params = call_kwargs.kwargs["params"]
    assert params.get("engines") == "google,stackoverflow"


@pytest.mark.asyncio
async def test_web_search_time_range_passed() -> None:
    """time_range parameter is forwarded to SearXNG query params."""
    resp = _mock_searxng_response()
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        await web_search_executor(query="openai news", time_range="week")

    call_kwargs = client.get.call_args
    params = call_kwargs.kwargs["params"]
    assert params.get("time_range") == "week"


@pytest.mark.asyncio
async def test_web_search_max_results_capped_at_50() -> None:
    """Requesting 100 results is silently capped at 50."""
    many_results = [
        {"title": f"r{i}", "url": f"https://example.com/{i}", "content": "", "engine": "g", "score": 0.5}
        for i in range(60)
    ]
    resp = _mock_searxng_response(results=many_results)
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="test", max_results=100)

    assert result["result_count"] <= 50


@pytest.mark.asyncio
async def test_web_search_infobox_handling() -> None:
    """Infoboxes are truncated to 2 entries with content capped at 500 chars."""
    long_content = "x" * 1000
    infoboxes = [
        {"infobox": "Python", "content": long_content, "urls": [{"url": "https://a.com"}, {"url": "https://b.com"}, {"url": "https://c.com"}, {"url": "https://d.com"}]},
        {"infobox": "Guido", "content": "Creator of Python", "urls": []},
        {"infobox": "Third", "content": "Should be dropped", "urls": []},
    ]
    resp = _mock_searxng_response(infoboxes=infoboxes)
    client = _mock_client(resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        result = await web_search_executor(query="python creator")

    assert len(result["infoboxes"]) == 2  # capped at 2
    assert len(result["infoboxes"][0]["content"]) <= 500  # content truncated
    assert len(result["infoboxes"][0]["urls"]) <= 3  # urls capped at 3


# ── Executor error tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_search_empty_query_raises() -> None:
    """Empty query string raises ToolExecutionError with descriptive message."""
    with pytest.raises(ToolExecutionError, match="query parameter is required"):
        await web_search_executor(query="")


@pytest.mark.asyncio
async def test_web_search_whitespace_query_raises() -> None:
    """Whitespace-only query raises ToolExecutionError."""
    with pytest.raises(ToolExecutionError, match="query parameter is required"):
        await web_search_executor(query="   ")


@pytest.mark.asyncio
async def test_web_search_connect_error_raises() -> None:
    """ConnectError raises ToolExecutionError with actionable message."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        with pytest.raises(ToolExecutionError, match="Cannot connect to SearXNG"):
            await web_search_executor(query="test query")


@pytest.mark.asyncio
async def test_web_search_timeout_raises() -> None:
    """TimeoutException raises ToolExecutionError mentioning timeout duration."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        with pytest.raises(ToolExecutionError, match="timed out"):
            await web_search_executor(query="test query")


@pytest.mark.asyncio
async def test_web_search_malformed_json_raises() -> None:
    """Non-JSON response body raises ToolExecutionError."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.side_effect = ValueError("not JSON")
    client = _mock_client(mock_resp)

    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        with pytest.raises(ToolExecutionError):
            await web_search_executor(query="test query")


# ── Governance / mode tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_search_blocked_in_lockdown_mode() -> None:
    """ToolExecutionLayer returns permission-denied ToolResult in LOCKDOWN mode."""
    from unittest.mock import patch as _patch

    from personal_agent.brainstem.mode_manager import ModeManager
    from personal_agent.config.governance_loader import load_governance_config
    from personal_agent.governance.models import Mode
    from personal_agent.telemetry import TraceContext
    from personal_agent.tools.executor import ToolExecutionLayer
    from personal_agent.tools.registry import ToolRegistry
    from personal_agent.tools.web import web_search_executor, web_search_tool

    reg = ToolRegistry()
    reg.register(web_search_tool, web_search_executor)

    gov = load_governance_config()
    mode_mgr = ModeManager(governance_config=gov)

    with _patch.object(mode_mgr, "get_current_mode", return_value=Mode.LOCKDOWN):
        layer = ToolExecutionLayer(registry=reg, governance_config=gov, mode_manager=mode_mgr)
        trace_ctx = TraceContext.new_trace()
        result = await layer.execute_tool("web_search", {"query": "test"}, trace_ctx)

    assert result.success is False
    assert result.error is not None
    assert "LOCKDOWN" in result.error or "not allowed" in result.error.lower()
