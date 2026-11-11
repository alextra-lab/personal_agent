"""Owner alerts of ``web_search`` (FRE-1768, FRE-1763 T3).

Owner decisions: no rescue provider, one ntfy alert per outage when both providers refuse
(5C), and one alert per calendar month when Exa plus Brave spend reaches
``web_search_monthly_budget_usd`` (soft $10, search continues). The sender
(``push_to_owner``) is mocked in every test, and the provider HTTP runs on
``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import SecretStr

from personal_agent.llm_client.cost_tracker import CostTrackerService
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools import web_search_alerts
from personal_agent.tools.web import (
    BRAVE_LLM_CONTEXT_URL,
    EXA_SEARCH_URL,
    WebSearchError,
    web_search_executor,
)

_CTX = TraceContext.new_trace()
_EXA_KEY = "test-exa-key-not-real"
_BRAVE_KEY = "test-brave-key-not-real"
_FIXTURES = Path(__file__).parent / "fixtures" / "web_search"
_ALERTS = "personal_agent.tools.web_search_alerts"

Reply = httpx.Response | Exception


def _ok(provider: str) -> httpx.Response:
    name = "exa_search.json" if provider == "exa" else "brave_llm_context.json"
    return httpx.Response(200, json=json.loads((_FIXTURES / name).read_text()))


def _error(status: int, url: str) -> httpx.Response:
    return httpx.Response(status, json={"error": "x"}, request=httpx.Request("GET", url))


_EXA_402 = _error(402, EXA_SEARCH_URL)
_BRAVE_429 = _error(429, BRAVE_LLM_CONTEXT_URL)


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("personal_agent.tools.web.settings.exa_api_key", SecretStr(_EXA_KEY))
    monkeypatch.setattr("personal_agent.tools.web.settings.brave_api_key", SecretStr(_BRAVE_KEY))


@pytest.fixture
def push() -> Iterator[AsyncMock]:
    """The owner alert sender, mocked; it reports a delivered alert."""
    sender = AsyncMock(return_value=True)
    with patch(f"{_ALERTS}.push_to_owner", sender):
        yield sender


@pytest.fixture
def month_total() -> Iterator[AsyncMock]:
    """The month's Exa plus Brave ``api_costs`` total, mocked (below budget by default)."""
    total = AsyncMock(return_value=0.0)
    tracker = MagicMock()
    tracker.get_cost_since = total
    with patch(f"{_ALERTS}.get_cost_tracker_service", return_value=tracker):
        yield total


async def _search(exa: Reply | None = None, brave: Reply | None = None) -> dict | None:
    """Run one search; return the result, or ``None`` when it raised ``WebSearchError``."""
    replies = {"exa": exa or _ok("exa"), "brave": brave or _ok("brave")}

    def handle(request: httpx.Request) -> httpx.Response:
        reply = replies["exa" if request.url.host == "api.exa.ai" else "brave"]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def client(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    with (
        patch("personal_agent.tools.web.create_guarded_http_client", client),
        patch("personal_agent.tools.web.record_vendor_cost", AsyncMock()),
    ):
        try:
            result = await web_search_executor(query="q", ctx=_CTX)
        except WebSearchError:
            result = None
    await web_search_alerts.wait_for_pending_alerts()
    return result


def _messages(push: AsyncMock) -> list[str]:
    return [call.args[0] for call in push.await_args_list]


# ── AC-1: one alert per outage when both providers refuse ──────────────────


@pytest.mark.asyncio
async def test_both_refusing_sends_one_actionable_alert(
    push: AsyncMock, month_total: AsyncMock
) -> None:
    """AC-1 / AC-4: what failed, each provider's error on its own line, what to do."""
    assert await _search(exa=_EXA_402, brave=_BRAVE_429) is None

    [message] = _messages(push)
    lines = message.splitlines()
    assert "both providers refused" in lines[0]
    assert "exa: HTTP 402" in lines
    assert "brave: HTTP 429" in lines
    assert "credit" in message and "API key" in message
    assert push.await_args.kwargs["trace_id"] == _CTX.trace_id
    for key in (_EXA_KEY, _BRAVE_KEY):
        assert key not in message


@pytest.mark.asyncio
async def test_one_alert_per_outage_rearmed_by_a_successful_search(
    push: AsyncMock, month_total: AsyncMock
) -> None:
    """AC-1: further both-refuse failures stay silent until a search succeeds."""
    for _ in range(3):
        await _search(exa=_EXA_402, brave=_BRAVE_429)
    assert push.await_count == 1

    assert await _search() is not None  # the outage ends
    assert push.await_count == 1

    await _search(exa=httpx.ConnectError("refused"), brave=_error(503, BRAVE_LLM_CONTEXT_URL))
    assert push.await_count == 2
    assert "exa: connection failed" in _messages(push)[1].splitlines()


@pytest.mark.asyncio
async def test_a_search_with_one_provider_serving_rearms_the_alert(
    push: AsyncMock, month_total: AsyncMock
) -> None:
    """AC-1: a search that returned results from one provider is a successful search."""
    await _search(exa=_EXA_402, brave=_BRAVE_429)
    await _search(exa=_EXA_402)
    await _search(exa=_EXA_402, brave=_BRAVE_429)
    assert push.await_count == 2


@pytest.mark.asyncio
async def test_an_undelivered_alert_rearms(push: AsyncMock, month_total: AsyncMock) -> None:
    """A failed ntfy send did not reach the owner, so the next both-refuse failure tries again."""
    push.return_value = False
    await _search(exa=_EXA_402, brave=_BRAVE_429)
    await _search(exa=_EXA_402, brave=_BRAVE_429)
    assert push.await_count == 2


# ── AC-2: no alert for a bad query or one failing provider ─────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exa", "brave"),
    [
        pytest.param(
            _error(400, EXA_SEARCH_URL), _error(422, BRAVE_LLM_CONTEXT_URL), id="bad-query"
        ),
        pytest.param(_EXA_402, None, id="exa-alone"),
        pytest.param(None, _BRAVE_429, id="brave-alone"),
        pytest.param(_error(400, EXA_SEARCH_URL), _BRAVE_429, id="bad-query-and-refusal"),
    ],
)
async def test_no_alert_unless_both_providers_refuse(
    push: AsyncMock, month_total: AsyncMock, exa: Reply | None, brave: Reply | None
) -> None:
    """AC-2: a bad query, or one provider failing, sends nothing."""
    await _search(exa=exa, brave=brave)
    push.assert_not_awaited()


# ── AC-3: one alert per month when spend reaches the budget ────────────────


def _at(year: int, month: int) -> patch:
    return patch(f"{_ALERTS}._now", return_value=datetime(year, month, 11, 9, 0, tzinfo=UTC))


@pytest.mark.asyncio
async def test_spend_reaching_the_budget_sends_one_alert_and_search_continues(
    push: AsyncMock, month_total: AsyncMock
) -> None:
    """AC-3: the search that takes the month's total to $10 alerts; the result still returns."""
    month_total.return_value = 10.004  # this search (0.007 + 0.005) crossed $10.00
    with _at(2026, 11):
        result = await _search()

    assert result is not None and result["result_count"] > 0
    [message] = _messages(push)
    assert "2026-11" in message and "$10.00" in message
    assert "continues" in message
    assert "credit" in message
    providers, since = month_total.await_args.args
    assert sorted(providers) == ["brave", "exa"]
    assert since == datetime(2026, 11, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_budget_alert_fires_once_per_month(push: AsyncMock, month_total: AsyncMock) -> None:
    """AC-3: later searches of the month stay silent; the next month can alert again."""
    with _at(2026, 11):
        month_total.return_value = 10.004
        await _search()
        month_total.return_value = 10.016
        await _search()
        month_total.return_value = 10.004  # a concurrent writer's sum, already alerted
        await _search()
    assert push.await_count == 1

    with _at(2026, 12):
        month_total.return_value = 10.001
        await _search()
    assert push.await_count == 2
    assert "2026-12" in _messages(push)[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "total",
    [
        pytest.param(9.99, id="below-budget"),
        pytest.param(10.5, id="crossed-before-this-search"),
    ],
)
async def test_no_budget_alert_unless_this_search_crossed(
    push: AsyncMock, month_total: AsyncMock, total: float
) -> None:
    """AC-3: below the budget, or already over it before this search (a restart), is silent."""
    month_total.return_value = total
    await _search()
    push.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_counts_only_the_providers_that_served(
    push: AsyncMock, month_total: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crossing check subtracts only this search's billed calls (Brave alone: $0.005)."""
    monkeypatch.setattr("personal_agent.tools.web.settings.web_search_monthly_budget_usd", 10.0)
    month_total.return_value = 10.006  # minus 0.005 is over; minus 0.012 would be under
    await _search(exa=_EXA_402)
    push.assert_not_awaited()

    month_total.return_value = 10.004
    await _search(exa=_EXA_402)
    assert push.await_count == 1


@pytest.mark.asyncio
async def test_a_failed_search_checks_no_budget(push: AsyncMock, month_total: AsyncMock) -> None:
    """No provider billed, so there is nothing to add up."""
    await _search(exa=_error(400, EXA_SEARCH_URL), brave=_error(422, BRAVE_LLM_CONTEXT_URL))
    month_total.assert_not_awaited()


# ── The month total query ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_cost_since_sums_the_named_providers_from_a_date() -> None:
    """One SUM over ``api_costs`` for the given providers since the given time."""
    tracker = CostTrackerService()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=10.25)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    pool.is_closing = MagicMock(return_value=False)
    tracker.pool = pool  # type: ignore[assignment]
    since = datetime(2026, 11, 1, tzinfo=UTC)

    assert await tracker.get_cost_since(("exa", "brave"), since) == 10.25

    sql, providers, start = conn.fetchval.await_args.args
    assert "SUM(cost_usd)" in sql and "api_costs" in sql
    assert providers == ["exa", "brave"]
    assert start == since


@pytest.mark.asyncio
async def test_get_cost_since_is_zero_without_a_pool() -> None:
    """No pool (no cost was written in this process) → 0.0, never a connection attempt."""
    assert await CostTrackerService().get_cost_since(("exa",), datetime.now(UTC)) == 0.0
