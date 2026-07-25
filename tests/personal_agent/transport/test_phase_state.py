"""Session-keyed current-phase projection (ADR-0123 §6, FRE-986).

The phase surface must be a *projection of current phase state*, not an accumulation of the
event log: a full-state ``phase_state`` snapshot keyed by session (mirroring ``turn_status``),
so a reconnecting client converges from the newest message alone and self-corrects when a
``PhaseEnd`` is dropped (AC-3).

These pin the server half — the in-process registry and the snapshot emitted on every phase
transition — exercising the **real** ``_persist_and_enqueue`` / ``_get_emit_lock`` path (only the
Postgres buffer + live queue are faked) so the seq-ordering and race-freedom guarantees are real:

- a phase transition emits a ``phase_state`` STATE_DELTA whose ``active`` set matches the registry,
  with a ``seq`` strictly after its own delta (delta-before-snapshot);
- the **highest-seq** snapshot always reflects the final registry state (race-freedom under the lock);
- a dropped ``PhaseEnd`` is still followed by a persisted higher-seq empty snapshot (self-correction);
- the session-sticky cap degrades a new session to delta-only rather than emitting a false empty
  snapshot for a tracked session;
- a held emit lock is never evicted from the lock cache (the one-stable-lock invariant);
- **AC-6** — a failing emit path never propagates.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import personal_agent.transport.agui.transport as transport_mod
from personal_agent.transport.agui.transport import (
    emit_phase_end,
    emit_phase_start,
    phase_span,
)
from personal_agent.transport.events import Phase

pytestmark = pytest.mark.asyncio

_SID = "11111111-1111-1111-1111-111111111111"
_SID2 = "22222222-2222-2222-2222-222222222222"


class _FakeBuffer:
    """Assigns a global monotonic seq, standing in for the Postgres sequence."""

    seq = 0

    def __init__(self, _db: Any) -> None:
        pass

    async def append(self, *, session_id: Any, event_type: str, payload: dict[str, Any]) -> int:
        # Yield so concurrent emitters interleave at the append point (inside the lock).
        await asyncio.sleep(0)
        _FakeBuffer.seq += 1
        return _FakeBuffer.seq


class _FakeDBCtx:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_a: Any) -> bool:
        return False


class _CaptureQueue:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def put_nowait(self, env: dict[str, Any]) -> None:
        self.items.append(env)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> dict[str, _CaptureQueue]:
    """Fake the persistence + live queue; exercise the real locked emit path.

    Returns the per-session capture queues; each holds the enqueued envelopes
    (delta + snapshot) in seq order, with ``seq`` stamped as in production.
    """
    _FakeBuffer.seq = 0
    transport_mod._phase_registry.clear()
    transport_mod._session_emit_locks.clear()
    queues: dict[str, _CaptureQueue] = {}

    def _get_queue(session_id: str) -> _CaptureQueue:
        return queues.setdefault(session_id, _CaptureQueue())

    monkeypatch.setattr(transport_mod, "SessionEventBuffer", _FakeBuffer)
    monkeypatch.setattr(transport_mod, "AsyncSessionLocal", lambda: _FakeDBCtx())
    monkeypatch.setattr(transport_mod, "get_event_queue", _get_queue)
    return queues


def _snapshots(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in items if e["type"] == "STATE_DELTA" and e["data"]["key"] == "phase_state"]


def _active_ids(snapshot_env: dict[str, Any]) -> set[str]:
    return {e["phase_id"] for e in snapshot_env["data"]["value"]["active"]}


class TestSnapshotEmission:
    async def test_start_emits_snapshot_after_delta(self, wire: dict[str, _CaptureQueue]) -> None:
        await emit_phase_start(
            session_id=_SID,
            phase=Phase.PLANNING,
            phase_id="p1",
            started_at="2026-07-25T10:00:00+00:00",
            detail="d",
            parent_id=None,
        )
        items = wire[_SID].items
        # delta first, snapshot second, snapshot seq strictly greater.
        assert items[0]["type"] == "PHASE_START"
        snaps = _snapshots(items)
        assert len(snaps) == 1
        assert snaps[0]["seq"] > items[0]["seq"]
        entry = snaps[0]["data"]["value"]["active"]
        assert entry == [
            {
                "phase": "planning",
                "phase_id": "p1",
                "started_at": "2026-07-25T10:00:00+00:00",
                "detail": "d",
                "parent_id": None,
            }
        ]

    async def test_concurrent_children_then_end_one(self, wire: dict[str, _CaptureQueue]) -> None:
        await emit_phase_start(
            session_id=_SID,
            phase=Phase.EXPANSION,
            phase_id="parent",
            started_at="2026-07-25T10:00:00+00:00",
        )
        await emit_phase_start(
            session_id=_SID,
            phase=Phase.SUB_AGENT,
            phase_id="c1",
            started_at="2026-07-25T10:00:01+00:00",
            parent_id="parent",
        )
        await emit_phase_start(
            session_id=_SID,
            phase=Phase.SUB_AGENT,
            phase_id="c2",
            started_at="2026-07-25T10:00:02+00:00",
            parent_id="parent",
        )
        await emit_phase_end(
            session_id=_SID, phase=Phase.SUB_AGENT, phase_id="c1", parent_id="parent"
        )

        snaps = _snapshots(wire[_SID].items)
        assert _active_ids(snaps[-1]) == {"parent", "c2"}

    async def test_last_end_emits_empty_snapshot_and_clears_registry(
        self, wire: dict[str, _CaptureQueue]
    ) -> None:
        await emit_phase_start(
            session_id=_SID,
            phase=Phase.SYNTHESIS,
            phase_id="p1",
            started_at="2026-07-25T10:00:00+00:00",
        )
        await emit_phase_end(session_id=_SID, phase=Phase.SYNTHESIS, phase_id="p1")
        snaps = _snapshots(wire[_SID].items)
        assert snaps[-1]["data"]["value"]["active"] == []
        assert _SID not in transport_mod._phase_registry

    async def test_dropped_end_still_persists_higher_seq_empty_snapshot(
        self, wire: dict[str, _CaptureQueue]
    ) -> None:
        # AC-3 self-correction (server half): even if the PhaseEnd *delta* never reaches the
        # client, the end emits a full-state snapshot with a higher seq than the start, whose
        # newest-wins replay alone converges the client (client half in phase-state.test.ts).
        await emit_phase_start(
            session_id=_SID,
            phase=Phase.PLANNING,
            phase_id="p1",
            started_at="2026-07-25T10:00:00+00:00",
        )
        start_delta_seq = wire[_SID].items[0]["seq"]
        await emit_phase_end(session_id=_SID, phase=Phase.PLANNING, phase_id="p1")
        empty = [s for s in _snapshots(wire[_SID].items) if s["data"]["value"]["active"] == []]
        assert empty, "phase_end must persist an empty full-state snapshot"
        assert empty[-1]["seq"] > start_delta_seq


class TestRaceFreedom:
    async def test_highest_seq_snapshot_reflects_final_registry(
        self, wire: dict[str, _CaptureQueue]
    ) -> None:
        # Interleave concurrent transitions through the real per-session lock; the snapshot is
        # built inside the lock, so the highest-seq snapshot must equal the final registry state
        # (never a stale {} enqueued above a live start's snapshot).
        await asyncio.gather(
            emit_phase_start(
                session_id=_SID,
                phase=Phase.SUB_AGENT,
                phase_id="a",
                started_at="2026-07-25T10:00:00+00:00",
                parent_id="p",
            ),
            emit_phase_start(
                session_id=_SID,
                phase=Phase.SUB_AGENT,
                phase_id="b",
                started_at="2026-07-25T10:00:01+00:00",
                parent_id="p",
            ),
            emit_phase_end(session_id=_SID, phase=Phase.SUB_AGENT, phase_id="a", parent_id="p"),
        )
        snaps = _snapshots(wire[_SID].items)
        top = max(snaps, key=lambda s: s["seq"])
        final = {e["phase_id"] for e in transport_mod._phase_snapshot_value(_SID)["active"]}
        assert _active_ids(top) == final


class TestCapDegradesWithoutFalseAuthority:
    async def test_new_session_over_cap_emits_no_snapshot(
        self, wire: dict[str, _CaptureQueue], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(transport_mod, "_MAX_PHASE_SESSIONS", 1)
        # Session 1 occupies the single slot with a live phase.
        await emit_phase_start(
            session_id=_SID,
            phase=Phase.PLANNING,
            phase_id="p1",
            started_at="2026-07-25T10:00:00+00:00",
        )
        # Session 2 is a brand-new session at cap → delta only, NO snapshot (no false empty).
        await emit_phase_start(
            session_id=_SID2,
            phase=Phase.PLANNING,
            phase_id="q1",
            started_at="2026-07-25T10:00:00+00:00",
        )
        await emit_phase_end(session_id=_SID2, phase=Phase.PLANNING, phase_id="q1")
        assert _snapshots(wire[_SID2].items) == []
        assert any(e["type"] == "PHASE_START" for e in wire[_SID2].items)
        # Session 1 (tracked) is unaffected — never evicted.
        assert _SID in transport_mod._phase_registry


class TestLockEvictionSkipsHeld:
    async def test_held_lock_not_evicted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        transport_mod._session_emit_locks.clear()
        monkeypatch.setattr(transport_mod, "_MAX_EMIT_LOCKS", 1)
        held = transport_mod._get_emit_lock(_SID)
        async with held:
            # A new session's lock is requested while _SID's lock is held; the cache is at cap,
            # but the held lock must not be evicted (one-stable-lock invariant, FRE-518).
            transport_mod._get_emit_lock(_SID2)
            assert transport_mod._get_emit_lock(_SID) is held


class TestBestEffort:
    async def test_emit_never_propagates(
        self, wire: dict[str, _CaptureQueue], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("persist down")

        monkeypatch.setattr(transport_mod, "_persist_and_enqueue", _boom)
        # AC-6: neither the delta nor the snapshot failing may fail the turn; both return None.
        await emit_phase_start(
            session_id=_SID,
            phase=Phase.PLANNING,
            phase_id="p1",
            started_at="2026-07-25T10:00:00+00:00",
        )
        await emit_phase_end(session_id=_SID, phase=Phase.PLANNING, phase_id="p1")
        # Registry mutation is authoritative regardless of emit failure.
        assert _SID not in transport_mod._phase_registry

    async def test_phase_span_still_pairs_and_snapshots(
        self, wire: dict[str, _CaptureQueue]
    ) -> None:
        async with phase_span(session_id=_SID, phase=Phase.SYNTHESIS) as pid:
            assert pid is not None
        types = [e["type"] for e in wire[_SID].items]
        assert types.count("PHASE_START") == 1
        assert types.count("PHASE_END") == 1
        # A span brackets the phase with a start snapshot and a final empty snapshot.
        assert _snapshots(wire[_SID].items)[-1]["data"]["value"]["active"] == []
