"""Unit tests for the fetch_url session failure memo (FRE-1672).

A URL that failed in a session is not fetched again by the same session: a non-retryable
failure returns the stored error at once, and a retryable failure gets one more real attempt.
No network access is used: ``create_guarded_http_client`` is mocked, and the mock's ``get``
call count is the count of real HTTP requests.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools import fetch
from personal_agent.tools.fetch import FetchError, fetch_url_executor

_URL = "https://example.com/page"
_ALREADY_FAILED = "already failed in this session"


def _ctx(session_id: str | None = "session-a") -> TraceContext:
    return TraceContext.new_trace(session_id=session_id)


def _response(body: str = "<p>Hello</p>", status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_error = status_code >= 400
    resp.text = body
    resp.content = body.encode()
    resp.headers = {"content-type": "text/html; charset=utf-8"}
    return resp


def _client(*outcomes: MagicMock | Exception) -> AsyncMock:
    """A mock HTTP client whose ``get`` yields each outcome in turn (raising exceptions)."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=list(outcomes))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _patched(client: AsyncMock):  # type: ignore[no-untyped-def]
    return patch("personal_agent.tools.fetch.create_guarded_http_client", return_value=client)


@pytest.fixture(autouse=True)
def _clear_memo() -> Iterator[None]:
    fetch._failed_fetches.clear()
    yield
    fetch._failed_fetches.clear()


@pytest.mark.asyncio
async def test_second_call_after_404_makes_no_request() -> None:
    """AC-1: the second call for a URL that returned 404 makes no request, returns the error."""
    client = _client(_response(status_code=404))
    with _patched(client):
        with pytest.raises(FetchError, match="HTTP 404"):
            await fetch_url_executor(url=_URL, ctx=_ctx())
        with pytest.raises(FetchError) as second:
            await fetch_url_executor(url=_URL, ctx=_ctx())

    assert client.get.await_count == 1
    assert _ALREADY_FAILED in second.value.message
    assert "HTTP 404" in second.value.message
    assert second.value.code == "http_error"
    assert second.value.retryable is False


@pytest.mark.asyncio
async def test_timeout_gets_one_more_attempt_then_stored_error() -> None:
    """AC-2: a timed-out URL is fetched once more, and the third call makes no request."""
    client = _client(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))
    with _patched(client):
        with pytest.raises(FetchError) as first:
            await fetch_url_executor(url=_URL, ctx=_ctx())
        assert first.value.retryable is True
        with pytest.raises(FetchError, match="timed out") as second:
            await fetch_url_executor(url=_URL, ctx=_ctx())
        assert _ALREADY_FAILED not in second.value.message
        with pytest.raises(FetchError) as third:
            await fetch_url_executor(url=_URL, ctx=_ctx())

    assert client.get.await_count == 2
    assert _ALREADY_FAILED in third.value.message
    assert "timed out" in third.value.message
    assert third.value.code == "upstream_timeout"
    assert third.value.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lambda: httpx.ConnectError("refused"), id="connect_failed"),
        pytest.param(lambda: _response(status_code=429), id="http_429"),
        pytest.param(lambda: _response(status_code=503), id="http_503"),
    ],
)
async def test_each_retryable_class_gets_one_more_attempt(
    failure: Callable[[], MagicMock | Exception],
) -> None:
    """Connect failure, 429 and 5xx are retryable: two real attempts, then a stored refusal."""
    client = _client(failure(), failure())
    with _patched(client):
        for _ in range(2):
            with pytest.raises(FetchError) as real:
                await fetch_url_executor(url=_URL, ctx=_ctx())
            assert real.value.retryable is True
            assert _ALREADY_FAILED not in real.value.message
        with pytest.raises(FetchError) as refused:
            await fetch_url_executor(url=_URL, ctx=_ctx())

    assert client.get.await_count == 2
    assert _ALREADY_FAILED in refused.value.message
    assert refused.value.retryable is False


@pytest.mark.asyncio
async def test_different_session_fetches_again() -> None:
    """AC-3: a different session reaches the network for a URL another session failed."""
    client = _client(_response(status_code=404), _response(status_code=404))
    with _patched(client):
        with pytest.raises(FetchError):
            await fetch_url_executor(url=_URL, ctx=_ctx("session-a"))
        with pytest.raises(FetchError) as other:
            await fetch_url_executor(url=_URL, ctx=_ctx("session-b"))

    assert client.get.await_count == 2
    assert _ALREADY_FAILED not in other.value.message


@pytest.mark.asyncio
async def test_different_url_in_same_session_is_not_blocked() -> None:
    client = _client(_response(status_code=404), _response("<p>ok</p>"))
    with _patched(client):
        with pytest.raises(FetchError):
            await fetch_url_executor(url=_URL, ctx=_ctx())
        result = await fetch_url_executor(url=_URL + "-other", ctx=_ctx())

    assert client.get.await_count == 2
    assert "ok" in result["text"]


@pytest.mark.asyncio
async def test_no_session_id_disables_the_memo() -> None:
    """Without a session there is no scope for a memo, so every call is a real fetch."""
    client = _client(*[_response(status_code=404)] * 3)
    with _patched(client):
        for _ in range(3):
            with pytest.raises(FetchError) as exc:
                await fetch_url_executor(url=_URL, ctx=_ctx(None))
            assert _ALREADY_FAILED not in exc.value.message

    assert client.get.await_count == 3


@pytest.mark.asyncio
async def test_no_readable_text_is_memoized() -> None:
    """A failure raised after the download (empty page) is stored like a download failure."""
    client = _client(_response("<script>x()</script>"))
    with _patched(client):
        with pytest.raises(FetchError, match="no readable text"):
            await fetch_url_executor(url=_URL, ctx=_ctx())
        with pytest.raises(FetchError) as second:
            await fetch_url_executor(url=_URL, ctx=_ctx())

    assert client.get.await_count == 1
    assert _ALREADY_FAILED in second.value.message
    assert second.value.code == "no_readable_text"


@pytest.mark.asyncio
async def test_success_clears_a_retryable_failure() -> None:
    """A success after a timeout drops the entry, so a later failure starts a fresh count."""
    client = _client(
        httpx.ReadTimeout("slow"),
        _response("<p>ok</p>"),
        httpx.ReadTimeout("slow"),
        httpx.ReadTimeout("slow"),
    )
    with _patched(client):
        with pytest.raises(FetchError):
            await fetch_url_executor(url=_URL, ctx=_ctx())
        await fetch_url_executor(url=_URL, ctx=_ctx())
        for _ in range(2):
            with pytest.raises(FetchError) as exc:
                await fetch_url_executor(url=_URL, ctx=_ctx())
            assert _ALREADY_FAILED not in exc.value.message

    assert client.get.await_count == 4


@pytest.mark.asyncio
async def test_session_cap_evicts_the_oldest_session() -> None:
    client = _client(*[_response(status_code=404)] * 4)
    with _patched(client), patch.object(fetch, "_MEMO_MAX_SESSIONS", 2):
        for session in ("s1", "s2", "s3"):
            with pytest.raises(FetchError):
                await fetch_url_executor(url=_URL, ctx=_ctx(session))
        # s1 left the memo, so it reaches the network; s3 is still stored.
        with pytest.raises(FetchError) as s1:
            await fetch_url_executor(url=_URL, ctx=_ctx("s1"))
        with pytest.raises(FetchError) as s3:
            await fetch_url_executor(url=_URL, ctx=_ctx("s3"))

    assert client.get.await_count == 4
    assert _ALREADY_FAILED not in s1.value.message
    assert _ALREADY_FAILED in s3.value.message


@pytest.mark.asyncio
async def test_url_cap_evicts_the_oldest_url_in_a_session() -> None:
    client = _client(*[_response(status_code=404)] * 4)
    with _patched(client), patch.object(fetch, "_MEMO_MAX_URLS", 2):
        for suffix in ("a", "b", "c"):
            with pytest.raises(FetchError):
                await fetch_url_executor(url=f"{_URL}-{suffix}", ctx=_ctx())
        with pytest.raises(FetchError) as a:
            await fetch_url_executor(url=f"{_URL}-a", ctx=_ctx())
        with pytest.raises(FetchError) as c:
            await fetch_url_executor(url=f"{_URL}-c", ctx=_ctx())

    assert client.get.await_count == 4
    assert _ALREADY_FAILED not in a.value.message
    assert _ALREADY_FAILED in c.value.message
