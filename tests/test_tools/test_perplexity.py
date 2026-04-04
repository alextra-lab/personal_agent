"""Unit tests for perplexity_query native tool (ADR-0028 Phase 2).

Tests use mocked httpx responses — no Perplexity API key required.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.perplexity import perplexity_query_executor, perplexity_query_tool


def _mock_response(status_code: int = 200, json_body: dict | None = None) -> MagicMock:
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = status_code
    mock_resp.is_error = status_code >= 400
    mock_resp.json.return_value = json_body or {}
    mock_resp.text = ""
    return mock_resp


def _mock_client(response: MagicMock) -> AsyncMock:
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _perplexity_response(answer: str, citations: list[str] | None = None) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": answer}}],
        "citations": citations or [],
        "model": "sonar",
    }


# ── Tool definition tests ──────────────────────────────────────────────────


def test_tool_definition() -> None:
    """Tool has correct metadata."""
    assert perplexity_query_tool.name == "perplexity_query"
    assert perplexity_query_tool.category == "network"
    assert perplexity_query_tool.risk_level == "medium"
    assert "NORMAL" in perplexity_query_tool.allowed_modes
    assert "DEGRADED" in perplexity_query_tool.allowed_modes
    assert "ALERT" not in perplexity_query_tool.allowed_modes
    param_names = {p.name for p in perplexity_query_tool.parameters}
    assert {"query", "mode"} <= param_names


# ── Validation tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_query_raises() -> None:
    """Empty query raises ToolExecutionError before any HTTP call."""
    with pytest.raises(ToolExecutionError, match="'query' parameter is required"):
        await perplexity_query_executor(query="")


@pytest.mark.asyncio
async def test_invalid_mode_raises() -> None:
    """Invalid mode raises ToolExecutionError before any HTTP call."""
    with pytest.raises(ToolExecutionError, match="Invalid mode"):
        await perplexity_query_executor(query="hello", mode="ultra")


@pytest.mark.asyncio
async def test_missing_api_key_raises() -> None:
    """Missing API key raises ToolExecutionError before any HTTP call."""
    with patch("personal_agent.tools.perplexity.settings") as mock_settings:
        mock_settings.perplexity_api_key = None
        with pytest.raises(ToolExecutionError, match="API key not configured"):
            await perplexity_query_executor(query="What is Python?")


# ── Success path tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ask_mode_success() -> None:
    """ask mode calls sonar model and returns answer + citations."""
    body = _perplexity_response("Python is a programming language.", ["https://python.org"])
    resp = _mock_response(200, body)
    with patch("personal_agent.tools.perplexity.settings") as mock_settings:
        mock_settings.perplexity_api_key = "test-key"
        mock_settings.perplexity_base_url = "https://api.perplexity.ai"
        mock_settings.perplexity_timeout_seconds = 90
        with patch("personal_agent.tools.perplexity.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client(resp)
            result = await perplexity_query_executor(query="What is Python?", mode="ask")

    assert result["mode"] == "ask"
    assert result["model"] == "sonar"
    assert "Python" in result["answer"]
    assert result["citations"] == ["https://python.org"]


@pytest.mark.asyncio
async def test_reason_mode_uses_correct_model() -> None:
    """reason mode uses sonar-reasoning model."""
    body = _perplexity_response("Reasoning answer.")
    resp = _mock_response(200, body)
    with patch("personal_agent.tools.perplexity.settings") as mock_settings:
        mock_settings.perplexity_api_key = "test-key"
        mock_settings.perplexity_base_url = "https://api.perplexity.ai"
        mock_settings.perplexity_timeout_seconds = 90
        with patch("personal_agent.tools.perplexity.httpx.AsyncClient") as mock_cls:
            client = _mock_client(resp)
            mock_cls.return_value = client
            result = await perplexity_query_executor(query="Compare A vs B", mode="reason")

    assert result["model"] == "sonar-reasoning"
    assert result["mode"] == "reason"
    # Check the payload sent to the API
    call_args = client.post.call_args
    payload = call_args.kwargs.get("json") or call_args.args[1] if call_args.args else {}
    if isinstance(call_args.kwargs.get("json"), dict):
        assert call_args.kwargs["json"]["model"] == "sonar-reasoning"


@pytest.mark.asyncio
async def test_research_mode_uses_correct_model() -> None:
    """research mode uses sonar-deep-research model."""
    body = _perplexity_response("Deep research answer.")
    resp = _mock_response(200, body)
    with patch("personal_agent.tools.perplexity.settings") as mock_settings:
        mock_settings.perplexity_api_key = "test-key"
        mock_settings.perplexity_base_url = "https://api.perplexity.ai"
        mock_settings.perplexity_timeout_seconds = 90
        with patch("personal_agent.tools.perplexity.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client(resp)
            result = await perplexity_query_executor(query="Survey of LLMs", mode="research")

    assert result["model"] == "sonar-deep-research"


@pytest.mark.asyncio
async def test_default_mode_is_ask() -> None:
    """Omitting mode defaults to ask."""
    body = _perplexity_response("Default answer.")
    resp = _mock_response(200, body)
    with patch("personal_agent.tools.perplexity.settings") as mock_settings:
        mock_settings.perplexity_api_key = "test-key"
        mock_settings.perplexity_base_url = "https://api.perplexity.ai"
        mock_settings.perplexity_timeout_seconds = 90
        with patch("personal_agent.tools.perplexity.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client(resp)
            result = await perplexity_query_executor(query="Hello?")

    assert result["mode"] == "ask"


# ── Error path tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_error_raises() -> None:
    """Non-2xx API response raises ToolExecutionError."""
    resp = _mock_response(401, {"error": {"message": "Invalid API key"}})
    with patch("personal_agent.tools.perplexity.settings") as mock_settings:
        mock_settings.perplexity_api_key = "bad-key"
        mock_settings.perplexity_base_url = "https://api.perplexity.ai"
        mock_settings.perplexity_timeout_seconds = 90
        with patch("personal_agent.tools.perplexity.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_client(resp)
            with pytest.raises(ToolExecutionError, match="HTTP 401"):
                await perplexity_query_executor(query="What is Python?")


@pytest.mark.asyncio
async def test_connect_error_raises() -> None:
    """Connection failure raises ToolExecutionError."""
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    with patch("personal_agent.tools.perplexity.settings") as mock_settings:
        mock_settings.perplexity_api_key = "test-key"
        mock_settings.perplexity_base_url = "https://api.perplexity.ai"
        mock_settings.perplexity_timeout_seconds = 90
        with patch("personal_agent.tools.perplexity.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = client
            with pytest.raises(ToolExecutionError, match="Cannot connect"):
                await perplexity_query_executor(query="What is Python?")
