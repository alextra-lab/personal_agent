"""FRE-51 / FRE-158: await prior session DB write before hydrating next /chat turn.

When the event bus handles assistant-message appends, the next request must wait
until the previous turn's append completes (or is dead-lettered). A Future per
session is registered before ``request.completed`` is published and resolved
when the session-writer consumer succeeds or when the consumer gives up after
max retries (see ``ConsumerRunner`` dead-letter path).
"""

from __future__ import annotations

import asyncio

# session_id -> Future set when this turn's append is done (success or terminal failure)
_session_write_waiters: dict[str, asyncio.Future[None]] = {}


def register_session_write_waiter(session_id: str) -> asyncio.Future[None]:
    """Register a waiter for the in-flight append for this session.

    Call before publishing ``request.completed`` so a follow-up /chat can await
    the same Future via ``await_previous_session_write``.

    Args:
        session_id: Session UUID string.

    Returns:
        Future completed when the append path finishes successfully or after
        terminal consumer failure.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[None] = loop.create_future()
    _session_write_waiters[session_id] = fut
    return fut


async def await_previous_session_write(session_id: str) -> None:
    """Await the prior turn's append waiter, if any (FRE-51 ordering)."""
    fut = _session_write_waiters.pop(session_id, None)
    if fut is not None:
        await fut


def release_session_write_wait(session_id: str) -> None:
    """Complete the waiter for ``session_id`` if still pending.

    Called after a successful append or from the consumer after dead-lettering
    the session-writer path so the API never deadlocks.
    """
    fut = _session_write_waiters.get(session_id)
    if fut is not None and not fut.done():
        fut.set_result(None)
