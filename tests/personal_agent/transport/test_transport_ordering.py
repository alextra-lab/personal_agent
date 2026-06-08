"""Per-session emit ordering invariant for the AG-UI transport (FRE-518).

Regression guard for the live-render gap: two concurrent emitters (the main chat
coroutine pushing the final response delta + the ``cg:turn-projector`` consumer
task pushing ``turn_status``) persist on separate DB connections, so the
``await buf.append`` resume order can invert the Postgres ``seq`` order. When a
higher-seq event is enqueued before a lower-seq one, the sender's monotonic
``max_sent_seq`` guard and the client's ``lastSeq`` guard both permanently drop
the lower-seq event — orphaning the final response from the WS path
(``session_events`` still has it, so only a REST re-hydration surfaces it).

The fix serialises the ``persist → set seq → enqueue`` critical section per
session, restoring the invariant *enqueue order == seq order*. These tests pin
that invariant: with the seq inverted on resume, the drained queue must still be
strictly ascending by ``seq``.

See: docs/architecture_decisions/ADR-0075-websocket-transport.md, FRE-518.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest

import personal_agent.transport.agui.transport as transport_mod
from personal_agent.transport.agui.ws_endpoint import get_event_queue
from personal_agent.transport.events import StateUpdateEvent, TextDeltaEvent


class _SharedSeqBuffer:
    """Buffer stand-in that inverts the resume order of two concurrent appends.

    Assigns a shared monotonic ``seq`` at call entry, then sleeps so the lower-seq
    call resumes (and enqueues) *after* the higher-seq call.

    ``_push_event`` constructs a fresh ``SessionEventBuffer(db)`` per call, so the
    counter and store are class-level to be shared across the two concurrent
    emits. The lower-seq call sleeps longer, forcing resume inversion in the
    absence of a serialising lock.
    """

    counter: int = 0
    rows: list[dict[str, Any]] = []

    def __init__(self, db: Any) -> None:
        """Accept and ignore the db session (matches the real API)."""

    @classmethod
    def reset(cls) -> None:
        """Reset shared state between tests."""
        cls.counter = 0
        cls.rows = []

    async def append(self, session_id: Any, event_type: str, payload: dict[str, Any]) -> int:
        """Assign the next shared seq at entry, then sleep (lower seq sleeps longer)."""
        type(self).counter += 1
        seq = type(self).counter
        type(self).rows.append({"seq": seq, "event_type": event_type})
        # Lower seq sleeps longer → it resumes (and enqueues) after the higher
        # seq, reproducing the cross-connection resume inversion.
        await asyncio.sleep(0.05 if seq == 1 else 0.0)
        return seq


@asynccontextmanager
async def _fake_session() -> Any:
    """Async CM yielding a dummy db handle for ``async with AsyncSessionLocal()``."""
    yield None


def _install_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the transport module's buffer + session factory with the fakes."""
    _SharedSeqBuffer.reset()
    monkeypatch.setattr(transport_mod, "SessionEventBuffer", _SharedSeqBuffer)
    monkeypatch.setattr(transport_mod, "AsyncSessionLocal", lambda: _fake_session())


def _drain(queue: asyncio.Queue[Any]) -> list[Any]:
    """Drain a queue without blocking, returning items in FIFO order."""
    items: list[Any] = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


@pytest.mark.asyncio
async def test_concurrent_emits_are_enqueued_in_seq_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent _push_event calls must enqueue in ascending seq order.

    With the resume order inverted by the buffer, the unserialised path enqueues
    [seq=2, seq=1]; the per-session lock restores [seq=1, seq=2].
    """
    _install_fakes(monkeypatch)
    session_id = str(uuid4())

    # Concurrent emitters: the final response delta and a turn_status STATE_DELTA.
    await asyncio.gather(
        transport_mod._push_event(
            TextDeltaEvent(text="final response", session_id=session_id), session_id
        ),
        transport_mod._push_event(
            StateUpdateEvent(key="turn_status", value={"x": 1}, session_id=session_id),
            session_id,
        ),
    )

    enqueued = _drain(get_event_queue(session_id))
    seqs = [item["seq"] for item in enqueued]
    assert seqs == sorted(seqs), f"events enqueued out of seq order: {seqs}"
    assert seqs == [1, 2]


@pytest.mark.asyncio
async def test_emit_done_sentinel_follows_a_concurrent_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """emit_done persists the DONE row + enqueues the None sentinel under the lock.

    Run concurrently with a normal emit: the seq-bearing event must precede the
    None sentinel in the queue, and the DONE row must carry the highest seq.
    """
    _install_fakes(monkeypatch)
    session_id = str(uuid4())

    await asyncio.gather(
        transport_mod._push_event(
            StateUpdateEvent(key="turn_status", value={"x": 1}, session_id=session_id),
            session_id,
        ),
        transport_mod.emit_done(session_id),
    )

    enqueued = _drain(get_event_queue(session_id))
    # Exactly one seq-bearing event and one None sentinel, sentinel last.
    assert enqueued[-1] is None
    seq_items = [item for item in enqueued if item is not None]
    assert len(seq_items) == 1
    # DONE row persisted with the highest seq (serialised after the live emit).
    done_rows = [r for r in _SharedSeqBuffer.rows if r["event_type"] == "DONE"]
    assert done_rows, "emit_done must persist a DONE row"
    assert done_rows[-1]["seq"] == max(r["seq"] for r in _SharedSeqBuffer.rows)
