"""FRE-520: regression tests for the session-write waiter pop/get race.

The original implementation popped the Future in ``await_previous_session_write``,
making it unreachable to ``release_session_write_wait`` (which looked it up via
``get``) — a follow-up /chat turn arriving before the consumer's release awaited
an unresolvable Future forever. These tests encode the fixed ownership rule
(release side owns removal) and the bounded-wait safety valve.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from personal_agent.config import settings
from personal_agent.events import session_write_waiter as sww
from personal_agent.events.session_write_waiter import (
    await_previous_session_write,
    register_session_write_waiter,
    release_session_write_wait,
)

SESSION_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _clean_waiters() -> Iterator[None]:
    """Ensure no waiter state leaks between tests."""
    sww._session_write_waiters.clear()
    yield
    sww._session_write_waiters.clear()


@pytest.mark.asyncio
async def test_await_before_release_completes() -> None:
    """The FRE-520 race: await starts before release — must still complete.

    Original code popped the Future inside the await, so the later release
    found nothing and the awaiting turn hung forever.
    """
    register_session_write_waiter(SESSION_ID)
    task = asyncio.create_task(await_previous_session_write(SESSION_ID))
    await asyncio.sleep(0)  # let the awaiter start waiting first
    release_session_write_wait(SESSION_ID)
    await asyncio.wait_for(task, timeout=1.0)
    assert SESSION_ID not in sww._session_write_waiters


@pytest.mark.asyncio
async def test_release_before_await_returns_immediately() -> None:
    """Common ordering (append finishes before next turn) still works."""
    register_session_write_waiter(SESSION_ID)
    release_session_write_wait(SESSION_ID)
    await asyncio.wait_for(await_previous_session_write(SESSION_ID), timeout=1.0)


@pytest.mark.asyncio
async def test_await_without_waiter_is_noop() -> None:
    """First turn of a session has no waiter registered."""
    await asyncio.wait_for(await_previous_session_write(SESSION_ID), timeout=1.0)


@pytest.mark.asyncio
async def test_timeout_proceeds_and_cleans_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If release never arrives, the bounded wait logs and proceeds."""
    monkeypatch.setattr(settings, "session_write_wait_timeout_seconds", 0.05)
    register_session_write_waiter(SESSION_ID)
    await asyncio.wait_for(await_previous_session_write(SESSION_ID), timeout=1.0)
    assert SESSION_ID not in sww._session_write_waiters


@pytest.mark.asyncio
async def test_timeout_does_not_pop_fresh_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ABA guard: a timed-out awaiter must not remove a newer turn's waiter."""
    monkeypatch.setattr(settings, "session_write_wait_timeout_seconds", 0.05)
    register_session_write_waiter(SESSION_ID)
    task = asyncio.create_task(await_previous_session_write(SESSION_ID))
    await asyncio.sleep(0)  # awaiter holds a reference to the first Future
    fresh = register_session_write_waiter(SESSION_ID)  # newer turn re-registers
    await asyncio.wait_for(task, timeout=1.0)  # first awaiter times out
    assert sww._session_write_waiters.get(SESSION_ID) is fresh
    fresh.cancel()


@pytest.mark.asyncio
async def test_release_after_timeout_is_noop() -> None:
    """A release arriving after the awaiter timed out must not raise."""
    register_session_write_waiter(SESSION_ID)
    sww._session_write_waiters.pop(SESSION_ID)  # simulate post-timeout cleanup
    release_session_write_wait(SESSION_ID)


@pytest.mark.asyncio
async def test_release_idempotent() -> None:
    """Double release (consumer ack path + handler path) must not raise."""
    register_session_write_waiter(SESSION_ID)
    release_session_write_wait(SESSION_ID)
    release_session_write_wait(SESSION_ID)
