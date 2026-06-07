"""FRE-51 / FRE-158: await prior session DB write before hydrating next /chat turn.

When the event bus handles assistant-message appends, the next request must wait
until the previous turn's append completes (or is dead-lettered). A Future per
session is registered before ``request.completed`` is published and resolved
when the session-writer consumer succeeds or when the consumer gives up after
max retries (see ``ConsumerRunner`` dead-letter path).

Ownership rule (FRE-520): ``release_session_write_wait`` is the single owner of
waiter removal. The awaiter only reads the dict — popping there made the Future
unreachable to the release path and deadlocked the turn when the follow-up
request arrived before the consumer's release.
"""

from __future__ import annotations

import asyncio

from personal_agent.config import settings
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

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


async def await_previous_session_write(session_id: str, trace_id: str | None = None) -> None:
    """Await the prior turn's append waiter, if any (FRE-51 ordering).

    Looks the waiter up without removing it — removal belongs to
    ``release_session_write_wait`` (FRE-520). The await is bounded: on timeout
    the stale waiter is discarded and the turn proceeds — ordering is
    best-effort, availability wins.

    Args:
        session_id: Session UUID string.
        trace_id: Trace ID of the awaiting request, for timeout logging.
    """
    fut = _session_write_waiters.get(session_id)
    if fut is None:
        return
    try:
        await asyncio.wait_for(
            asyncio.shield(fut), timeout=settings.session_write_wait_timeout_seconds
        )
    except TimeoutError:
        # Identity-guarded: a newer turn may have re-registered under this key.
        if _session_write_waiters.get(session_id) is fut:
            del _session_write_waiters[session_id]
        log.warning(
            "session_write_wait_timeout",
            session_id=session_id,
            trace_id=trace_id,
            timeout_seconds=settings.session_write_wait_timeout_seconds,
        )


def release_session_write_wait(session_id: str) -> None:
    """Complete and remove the waiter for ``session_id`` if still pending.

    Single owner of waiter removal (FRE-520). Called after a successful append
    or from the consumer after dead-lettering the session-writer path so the
    API never deadlocks.
    """
    fut = _session_write_waiters.pop(session_id, None)
    if fut is not None and not fut.done():
        fut.set_result(None)
