"""The reaper updates counters in ascending id order across a batch (FRE-1733 AC-3).

``reap_stale`` holds every counter lock until its one transaction ends, so it must take them in the
order that ``reserve`` takes them. A fake connection records the order, so this runs in ``make test``.
The same sweep on real rows is in ``test_reaper.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from personal_agent.cost_gate import BudgetConfig, CostGate


@dataclass
class _FakeConnection:
    """Return the expired rows, and record each counter update."""

    expired: Sequence[dict[str, object]]
    updates: list[tuple[int, Decimal]] = field(default_factory=list)

    async def fetch(self, _query: str, *_args: object) -> Sequence[dict[str, object]]:
        return self.expired

    async def execute(self, query: str, amount: Decimal, counter_id: int) -> None:
        assert "UPDATE budget_counters" in query
        self.updates.append((counter_id, amount))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield


@dataclass
class _FakePool:
    connection: _FakeConnection

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_FakeConnection]:
        yield self.connection


def _reservation(amount: str, counter_ids: list[int]) -> dict[str, object]:
    return {
        "reservation_id": "r",
        "role": "a_role",
        "amount_usd": Decimal(amount),
        "counter_ids": counter_ids,
    }


@pytest.mark.asyncio
async def test_a_batch_updates_counters_in_ascending_order_with_one_update_each() -> None:
    """The ticket's case: a reservation with counters 10, 11 before one with 3, 11."""
    connection = _FakeConnection(
        expired=[_reservation("0.10", [10, 11]), _reservation("0.25", [3, 11])]
    )
    gate = CostGate(config=BudgetConfig(version=1, roles={}, caps=[]), db_url="unused")
    gate.pool = _FakePool(connection)

    assert await gate.reap_stale() == 2

    assert connection.updates == [
        (3, Decimal("0.25")),
        (10, Decimal("0.10")),
        (11, Decimal("0.35")),
    ]
