"""Owner alerts of ``web_search``: provider outage and monthly spend (FRE-1768, FRE-1763 T3).

Both alerts go through the gateway's owner ntfy channel (:func:`push_to_owner`, FRE-1758).

**Outage (owner decision 5C).** There is no rescue provider. When both providers refuse a search
(see ``FailureKind`` in :mod:`personal_agent.tools.web`), the owner gets one alert. Further
both-refuse failures stay silent until a search succeeds, which re-arms the alert. A bad query, a
provider failing alone, or a bad query plus a refusal sends nothing. The armed/fired flag lives in
this process's memory: a gateway restart re-arms it, so an outage that spans a restart can alert
once more. An alert that ntfy did not accept re-arms too, so the next failure tries again.

**Monthly spend (soft budget).** After a search writes its ``api_costs`` rows, one ``SUM`` reads
the UTC calendar month's Exa plus Brave total. The alert fires when this search took the total
from below ``web_search_monthly_budget_usd`` to at or above it. The ``api_costs`` rows are the
persistent "already alerted" mark: after a restart, a month already over budget does not cross
again, so it does not alert again. An in-process month mark stops two concurrent crossing searches
from both alerting. Search always continues.

The checks never raise into the search. The ntfy send runs as a background task, so a slow send
adds no time to the search and the tool timeout cannot cancel it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from personal_agent.config import settings
from personal_agent.llm_client.cost_tracker import get_cost_tracker_service
from personal_agent.service.owner_ntfy import push_to_owner
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

#: ``api_costs.provider`` values of the web search providers.
SEARCH_PROVIDERS: tuple[str, str] = ("exa", "brave")


class ProviderFailureLike(Protocol):
    """The fields of ``web.ProviderFailure`` this module reads."""

    @property
    def provider(self) -> str:
        """The provider name."""
        ...

    @property
    def kind(self) -> str:
        """``refusal`` or ``bad_query``."""
        ...

    @property
    def reason(self) -> str:
        """Short cause, such as ``HTTP 429``. Never holds a key."""
        ...


@dataclass
class _AlertState:
    """Process-wide alert state.

    Attributes:
        outage_alerted: An outage alert fired and no search has succeeded since.
        budget_alerted_month: The ``YYYY-MM`` this process already sent the budget alert for.
    """

    outage_alerted: bool = False
    budget_alerted_month: str | None = None


_state = _AlertState()
_pending: set[asyncio.Task[None]] = set()


def _now() -> datetime:
    """Return the current UTC time (a seam for tests)."""
    return datetime.now(UTC)


def _spawn(coro: Coroutine[object, object, None]) -> None:
    """Run ``coro`` in the background and hold a reference until it ends."""
    task = asyncio.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def outage_message(failures: Sequence[ProviderFailureLike]) -> str:
    """Build the outage alert text.

    Args:
        failures: Each provider's failure.

    Returns:
        What failed, one line per provider error, and what the owner can do.
    """
    lines = ["Seshat web_search is down: both providers refused."]
    lines.extend(f"{f.provider}: {f.reason}" for f in failures)
    lines.append(
        "Check Exa and Brave credit and API keys (AGENT_EXA_API_KEY, AGENT_BRAVE_API_KEY). "
        "No new alert until a search succeeds."
    )
    return "\n".join(lines)


def budget_message(month: str, total_usd: float, budget_usd: float) -> str:
    """Build the monthly budget alert text.

    Args:
        month: The ``YYYY-MM`` month.
        total_usd: The month's Exa plus Brave total.
        budget_usd: The configured budget.

    Returns:
        What was reached, and what the owner can do.
    """
    return (
        f"Seshat web_search spend for {month} reached ${total_usd:.2f} "
        f"(Exa + Brave, budget ${budget_usd:.2f}). Search continues.\n"
        "Check Exa and Brave usage and credit; AGENT_WEB_SEARCH_MONTHLY_BUDGET_USD sets the "
        "alert level."
    )


async def _send_outage_alert(message: str, trace_id: str) -> None:
    """Send the outage alert; re-arm when ntfy did not accept it."""
    sent = await push_to_owner(message, trace_id=trace_id)
    log.info("web_search_outage_alert", trace_id=trace_id, sent=sent)
    if not sent:
        _state.outage_alerted = False


def note_search_failed(failures: Sequence[ProviderFailureLike], *, trace_id: str) -> None:
    """Record a search that no provider served; alert once when both refused.

    Args:
        failures: Each provider's failure.
        trace_id: Trace id of the search.
    """
    if not failures or any(f.kind != "refusal" for f in failures):
        return
    if _state.outage_alerted:
        log.info("web_search_outage_alert_suppressed", trace_id=trace_id)
        return
    _state.outage_alerted = True
    _spawn(_send_outage_alert(outage_message(failures), trace_id))


async def _check_budget(served_cost_usd: float, trace_id: str) -> None:
    """Alert when this search took the month's Exa plus Brave total to the budget."""
    now = _now()
    month = now.strftime("%Y-%m")
    if _state.budget_alerted_month == month:
        return
    budget = settings.web_search_monthly_budget_usd
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = await get_cost_tracker_service().get_cost_since(SEARCH_PROVIDERS, start)
    if total < budget or total - served_cost_usd >= budget:
        return
    if _state.budget_alerted_month == month:  # a concurrent check alerted during the SUM
        return
    _state.budget_alerted_month = month
    sent = await push_to_owner(budget_message(month, total, budget), trace_id=trace_id)
    log.info(
        "web_search_budget_alert",
        trace_id=trace_id,
        month=month,
        total_usd=round(total, 4),
        budget_usd=budget,
        sent=sent,
    )


def note_search_served(*, served_cost_usd: float, trace_id: str) -> None:
    """Record a search that at least one provider served.

    Re-arms the outage alert, then checks the monthly budget in the background.

    Args:
        served_cost_usd: What this search's ``api_costs`` rows add up to.
        trace_id: Trace id of the search.
    """
    _state.outage_alerted = False
    _spawn(_check_budget(served_cost_usd, trace_id))


async def wait_for_pending_alerts() -> None:
    """Wait until every background alert task has finished."""
    while _pending:
        await asyncio.gather(*_pending, return_exceptions=True)


def reset_alert_state() -> None:
    """Forget the outage and budget marks (for tests)."""
    _state.outage_alerted = False
    _state.budget_alerted_month = None
