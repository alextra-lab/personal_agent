# ruff: noqa: D101, D102
"""The lease keeper's stop rule (ADR-0159 D9.5; FRE-1684 /code-review high findings 1 and 2).

Pure unit tests: a fake store and a manual clock. No database.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from personal_agent.dev_agent.jobs import Lease, TokenSignal
from personal_agent.dev_agent.supervisor import LeaseKeeper, SupervisorPolicy

from .supervisor_fakes import ManualClock

pytestmark = pytest.mark.asyncio

_POLICY = SupervisorPolicy()


class _Store:
    """A store whose renewals fail, or hang until the keeper's attempt timeout."""

    def __init__(self, *, hang: bool) -> None:
        self.hang = hang
        self.attempts = 0
        self.entered = asyncio.Event()

    async def renew(self, lease: Lease, lease_seconds: float) -> TokenSignal | None:
        del lease, lease_seconds
        self.attempts += 1
        self.entered.set()
        if self.hang:
            await asyncio.Event().wait()
        raise ConnectionError("seeded: database unreachable")


def _keeper(store: _Store, clock: ManualClock, policy: SupervisorPolicy = _POLICY) -> LeaseKeeper:
    lease = Lease(job_id=uuid4(), owner="sup-a", epoch=1)
    return LeaseKeeper(store, lease, policy, clock)  # type: ignore[arg-type]


async def _forever() -> None:
    await asyncio.Event().wait()


class TestStopRule:
    async def test_a_failed_renewal_is_retried_after_a_third_of_the_interval(self) -> None:
        clock, store = ManualClock(), _Store(hang=False)
        work = asyncio.create_task(_forever())
        keeper = _keeper(store, clock)
        keeping = asyncio.create_task(keeper.run(work))
        try:
            await asyncio.sleep(0)
            await clock.advance(30)
            assert store.attempts == 1
            assert clock.sleeps == [30, 10]
            await clock.advance(10)
            await clock.advance(10)
            assert store.attempts == 3
            assert not work.done(), "50 s without a renewal is inside the 60 s rule"
            await clock.advance(10)
            await asyncio.sleep(0)
            assert work.cancelled() and keeper.lost
        finally:
            work.cancel()
            keeping.cancel()

    async def test_a_renewal_that_waits_on_a_fenced_action_does_not_stop_the_work(self) -> None:
        """Finding 1: the renewal starts, then a fence takes the row and runs past the stop window."""
        policy = SupervisorPolicy(renew_seconds=0.3, stop_after_seconds=0.6)
        clock, store = ManualClock(), _Store(hang=True)
        work = asyncio.create_task(_forever())
        keeper = _keeper(store, clock, policy)
        keeping = asyncio.create_task(keeper.run(work))
        try:
            await asyncio.sleep(0)
            await clock.advance(0.3)
            await store.entered.wait()  # the renewal runs and blocks on the row
            with keeper.action():  # the fence holds the row
                clock.now += 1.0  # past the stop window
                await asyncio.sleep(0.3)  # the attempt times out (0.1 s real time)
                assert not work.done(), "no stop while a fenced action holds the row"
            assert not keeper.lost
        finally:
            work.cancel()
            keeping.cancel()
