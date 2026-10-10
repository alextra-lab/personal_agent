"""Unit tests for the get_library_docs native tool (ADR-0028 Phase 3, FRE-1575 contract).

Every HTTP call is mocked through ``create_guarded_http_client`` — no network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.security import EgressBlockedError
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.context7 import get_library_docs_executor, get_library_docs_tool
from personal_agent.tools.executor import ToolExecutionError

_CTX = TraceContext.new_trace()


def _response(
    status_code: int = 200, json_body: object = None, text: str = "", json_error: bool = False
) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_error = status_code >= 400
    if json_error:
        resp.json.side_effect = ValueError("Expecting value")
    else:
        resp.json.return_value = json_body
    resp.text = text
    return resp


def _client(
    *, responses: list[MagicMock] | None = None, error: Exception | None = None
) -> AsyncMock:
    client = AsyncMock()
    if error is not None:
        client.get = AsyncMock(side_effect=error)
    else:
        client.get = AsyncMock(side_effect=responses or [])
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


async def _fail(client: AsyncMock, library: str = "fastapi") -> ToolExecutionError:
    """Run one call against ``client`` and return the error it raised."""
    with patch("personal_agent.tools.context7.create_guarded_http_client", return_value=client):
        with pytest.raises(ToolExecutionError) as exc_info:
            await get_library_docs_executor(library=library, ctx=_CTX)
    return exc_info.value


_FOUND = {"results": [{"id": "/tiangolo/fastapi", "title": "FastAPI"}]}


def test_tool_declares_its_codes() -> None:
    """The definition declares the closed code set and a description with examples."""
    assert get_library_docs_tool.error_codes == frozenset(
        {
            "invalid_argument",
            "egress_blocked",
            "upstream_unavailable",
            "upstream_timeout",
            "upstream_failed",
            "upstream_rejected",
            "not_found",
            "bad_reply",
        }
    )
    assert "get_library_docs(library=" in get_library_docs_tool.description
    assert all(p.description for p in get_library_docs_tool.parameters)


@pytest.mark.asyncio
async def test_success_returns_docs() -> None:
    """A match and a docs fetch return the docs and the resolved name."""
    client = _client(responses=[_response(json_body=_FOUND), _response(text="# FastAPI docs")])
    with patch("personal_agent.tools.context7.create_guarded_http_client", return_value=client):
        result = await get_library_docs_executor(library="fastapi", topic="routing", ctx=_CTX)
    assert result["library_id"] == "/tiangolo/fastapi"
    assert result["library_name"] == "FastAPI"
    assert result["docs"] == "# FastAPI docs"
    assert client.get.call_args_list[1].kwargs["params"] == {"tokens": 5000, "topic": "routing"}


@pytest.mark.asyncio
async def test_empty_library_is_invalid_argument() -> None:
    """An empty library name is refused before any request."""
    with pytest.raises(ToolExecutionError) as exc_info:
        await get_library_docs_executor(library="  ", ctx=_CTX)
    assert exc_info.value.code == "invalid_argument"
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_no_match_is_not_found() -> None:
    """A search with no results is not_found, not retryable."""
    error = await _fail(_client(responses=[_response(json_body={"results": []})]), "nosuchlib")
    assert error.code == "not_found"
    assert error.retryable is False
    assert "nosuchlib" in str(error)


@pytest.mark.asyncio
async def test_match_without_id_is_bad_reply() -> None:
    """A match that carries no library ID is bad_reply."""
    error = await _fail(_client(responses=[_response(json_body={"results": [{"title": "X"}]})]))
    assert error.code == "bad_reply"
    assert error.retryable is False


@pytest.mark.asyncio
async def test_unparseable_search_reply_is_bad_reply() -> None:
    """A search reply that is not JSON, or not a JSON object, is a retryable bad_reply."""
    not_json = await _fail(_client(responses=[_response(json_error=True)]))
    not_object = await _fail(_client(responses=[_response(json_body=["a", "list"])]))
    assert (not_json.code, not_object.code) == ("bad_reply", "bad_reply")
    assert not_json.retryable and not_object.retryable


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "retryable"), [(404, False), (429, True), (500, True)])
async def test_search_http_error_is_upstream_rejected(status: int, retryable: bool) -> None:
    """An HTTP error on the search is upstream_rejected; only a 429 or 5xx is retryable."""
    error = await _fail(_client(responses=[_response(status)]))
    assert error.code == "upstream_rejected"
    assert error.retryable is retryable
    assert f"HTTP {status}" in str(error)


@pytest.mark.asyncio
async def test_docs_http_error_is_upstream_rejected() -> None:
    """An HTTP error on the docs fetch is upstream_rejected."""
    error = await _fail(_client(responses=[_response(json_body=_FOUND), _response(403)]))
    assert error.code == "upstream_rejected"
    assert error.retryable is False
    assert "docs fetch" in str(error)


@pytest.mark.asyncio
async def test_transport_failures_carry_codes() -> None:
    """Connect, timeout and other transport errors are retryable with distinct codes."""
    connect = await _fail(_client(error=httpx.ConnectError("refused")))
    timeout = await _fail(_client(error=httpx.TimeoutException("slow")))
    other = await _fail(_client(error=httpx.ReadError("reset")))
    assert [e.code for e in (connect, timeout, other)] == [
        "upstream_unavailable",
        "upstream_timeout",
        "upstream_failed",
    ]
    assert all(e.retryable for e in (connect, timeout, other))


@pytest.mark.asyncio
async def test_egress_refusal_is_egress_blocked() -> None:
    """An egress-guard refusal is egress_blocked and names web_search as the next call."""
    request = httpx.Request("GET", "https://context7.com/api/v1/search")
    error = await _fail(_client(error=EgressBlockedError(request, "domain not allowlisted", None)))
    assert error.code == "egress_blocked"
    assert error.retryable is False
    assert "web_search" in error.next_action
