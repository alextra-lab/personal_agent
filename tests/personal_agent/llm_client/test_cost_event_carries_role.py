"""FRE-989 AC-3: the Elasticsearch cost event names the role that spent.

The defect: ``api_cost_recorded`` carried cost, model, provider, trace, session,
latency and tokens — but **no** ``purpose``/role field. So Elasticsearch recorded
what a call cost and not which budget role spent it, making "what did
captains_log cost today" unanswerable from ES *by construction* rather than by
difficulty. The only role-attributed store was Postgres ``api_costs.purpose``.

It was also emitted at ``debug``, which is not a level a ledger record belongs
at. Both are fixed here; the audit doc states that ES is complete and
role-attributed only from this change forward.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from personal_agent.llm_client.cost_tracker import CostTrackerService


@pytest.fixture
def tracker_with_stub_pool() -> CostTrackerService:
    """A tracker whose pool accepts the INSERT and returns a row id."""
    tracker = CostTrackerService()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=4242)

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_ctx)
    tracker.pool = pool
    return tracker


def _cost_event(logs: list[dict[str, object]]) -> dict[str, object]:
    entries = [entry for entry in logs if entry.get("event") == "api_cost_recorded"]
    assert entries, f"no api_cost_recorded event emitted; saw {[e.get('event') for e in logs]}"
    return entries[0]


@pytest.mark.asyncio
async def test_cost_event_carries_the_budget_role(
    tracker_with_stub_pool: CostTrackerService,
) -> None:
    """AC-3: the role that spent is on the event, not only in Postgres."""
    with capture_logs() as logs:
        await tracker_with_stub_pool.record_api_call(
            provider="anthropic",
            model="anthropic/claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.0123,
            trace_id=uuid4(),
            session_id=uuid4(),
            purpose="insights",
        )

    assert _cost_event(logs)["purpose"] == "insights"


@pytest.mark.asyncio
async def test_cost_event_is_emitted_at_info(
    tracker_with_stub_pool: CostTrackerService,
) -> None:
    """A ledger record is not a debug detail — debug-level events ship unevenly."""
    with capture_logs() as logs:
        await tracker_with_stub_pool.record_api_call(
            provider="anthropic",
            model="anthropic/claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.0123,
            trace_id=uuid4(),
            session_id=uuid4(),
            purpose="captains_log",
        )

    assert _cost_event(logs)["log_level"] == "info"


@pytest.mark.asyncio
async def test_uncapped_role_still_records_its_spend(
    tracker_with_stub_pool: CostTrackerService,
) -> None:
    """AC-3: having no per-role cap must not mean having no measurement.

    ``insights``, ``promotion`` and ``freshness`` have no per-role cap entry, so
    the gate creates no per-role counter row for them. Measurement therefore
    cannot come from the counters — it comes from the ledger, which is written
    on every paid call regardless of whether any cap applies.
    """
    with capture_logs() as logs:
        record_id = await tracker_with_stub_pool.record_api_call(
            provider="anthropic",
            model="anthropic/claude-sonnet-4-6",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.0001,
            trace_id=uuid4(),
            session_id=uuid4(),
            purpose="freshness",
        )

    assert record_id == 4242
    assert _cost_event(logs)["purpose"] == "freshness"
