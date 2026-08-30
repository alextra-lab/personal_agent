"""AC-3 (FRE-1339) — fetch_url provenance against a prior web_search's result set.

``web_search_completed`` used to log only ``result_count``, so it was impossible to
tell whether a later ``fetch_url`` target came from a search result or was invented
by the model — the exact gap FRE-1330's novel-destination signal and the 2026-08-29
Alibaba-bucket incident both turned on. ``web.py`` now logs ``result_urls`` on that
event, which makes ``fetch_url.url in prior_search.result_urls`` a checkable
assertion. These tests run both tools' real executors (mocked HTTP only, no network)
rather than asserting against a hand-built dict, to demonstrate the join holds over
an actual call sequence sharing one trace_id — the shape of "a real turn".
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.fetch import fetch_url_executor
from personal_agent.tools.web import web_search_executor

_CTX = TraceContext.new_trace()


def _mock_searxng_response(results: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "results": results,
        "suggestions": [],
        "infoboxes": [],
        "unresponsive_engines": [],
    }
    return resp


def _mock_html_response(body: str) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.is_error = False
    resp.text = body
    resp.headers = {"content-type": "text/html; charset=utf-8"}
    return resp


def _mock_client(response: MagicMock) -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


async def _run_search_and_capture_result_urls(results: list[dict]) -> set[str]:
    """Run the real web_search_executor and return the URL set its telemetry recorded."""
    resp = _mock_searxng_response(results)
    client = _mock_client(resp)
    with patch("personal_agent.tools.web.httpx.AsyncClient", return_value=client):
        with patch("personal_agent.tools.web.log") as mock_log:
            await web_search_executor(query="python docs", ctx=_CTX)

    completed_call = next(
        c for c in mock_log.info.call_args_list if c.args[0] == "web_search_completed"
    )
    return set(completed_call.kwargs["result_urls"])


@pytest.mark.asyncio
async def test_fetch_url_target_found_in_prior_search_result_set() -> None:
    """Positive case: fetch_url's target was one of the prior search's own results."""
    result_urls = await _run_search_and_capture_result_urls(
        [
            {
                "title": "Python 3.12 docs",
                "url": "https://docs.python.org/3.12/",
                "content": "",
                "engine": "google",
            },
            {
                "title": "Real Python",
                "url": "https://realpython.com/",
                "content": "",
                "engine": "bing",
            },
        ]
    )

    target_url = "https://docs.python.org/3.12/"
    fetch_resp = _mock_html_response(
        "<html><body>Official Python 3.12 documentation.</body></html>"
    )
    with patch(
        "personal_agent.tools.fetch.create_guarded_http_client",
        return_value=_mock_client(fetch_resp),
    ):
        fetched = await fetch_url_executor(url=target_url, ctx=_CTX)

    assert fetched["url"] in result_urls


@pytest.mark.asyncio
async def test_fetch_url_target_absent_from_prior_search_result_set() -> None:
    """Negative case: a model-constructed URL that never appeared in the search results."""
    result_urls = await _run_search_and_capture_result_urls(
        [
            {
                "title": "Python 3.12 docs",
                "url": "https://docs.python.org/3.12/",
                "content": "",
                "engine": "google",
            }
        ]
    )

    constructed_url = "https://routify-file-proxy-sg.oss-ap-southeast-1.aliyuncs.com/not-a-result"
    fetch_resp = _mock_html_response("<html><body>unrelated page</body></html>")
    with patch(
        "personal_agent.tools.fetch.create_guarded_http_client",
        return_value=_mock_client(fetch_resp),
    ):
        fetched = await fetch_url_executor(url=constructed_url, ctx=_CTX)

    assert fetched["url"] not in result_urls
