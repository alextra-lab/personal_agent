"""Best-effort phase emitters + phase_span context manager (ADR-0123 §2, FRE-934).

These pin the transport-level guarantees the emission sites rely on:
- the helpers push the right ``InternalEvent`` on the shared ``_push_event`` path;
- **AC-6** — a failing emit path never propagates (a cosmetic loss, never a failed turn);
- ``phase_span`` is a no-op without a session, and pairs start/end on every exit.
"""

from __future__ import annotations

from typing import Any

import pytest

import personal_agent.transport.agui.transport as transport_mod
from personal_agent.transport.agui.transport import (
    emit_phase_end,
    emit_phase_start,
    phase_span,
)
from personal_agent.transport.events import Phase, PhaseEndEvent, PhaseStartEvent

pytestmark = pytest.mark.asyncio


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Capture the events handed to ``_push_event`` (bypassing Postgres/queue)."""
    captured: list[Any] = []

    async def _capture(event: Any, session_id: str) -> None:
        captured.append(event)

    async def _noop_snapshot(session_id: str, phase_id: str) -> None:
        # The full-state phase_state snapshot (FRE-986) has its own suite
        # (test_phase_state.py); stub it here so these delta-focused tests stay
        # hermetic and never touch the real persist path.
        return None

    monkeypatch.setattr(transport_mod, "_push_event", _capture)
    monkeypatch.setattr(transport_mod, "_emit_phase_snapshot_best_effort", _noop_snapshot)
    return captured


class TestEmitHelpers:
    async def test_emit_phase_start_pushes_event(self, recorder: list[Any]) -> None:
        await emit_phase_start(
            session_id="s1",
            phase=Phase.PLANNING,
            phase_id="p1",
            started_at="2026-07-25T10:00:00+00:00",
            detail="d",
            parent_id="parent",
        )
        assert len(recorder) == 1
        ev = recorder[0]
        assert isinstance(ev, PhaseStartEvent)
        assert ev.phase is Phase.PLANNING
        assert ev.phase_id == "p1"
        assert ev.parent_id == "parent"
        assert ev.started_at == "2026-07-25T10:00:00+00:00"

    async def test_emit_phase_end_pushes_event(self, recorder: list[Any]) -> None:
        await emit_phase_end(session_id="s1", phase=Phase.SYNTHESIS, phase_id="p1")
        assert len(recorder) == 1
        assert isinstance(recorder[0], PhaseEndEvent)
        assert recorder[0].phase is Phase.SYNTHESIS
        assert recorder[0].ok is True

    async def test_emit_phase_end_explicit_ok_false(self, recorder: list[Any]) -> None:
        await emit_phase_end(session_id="s1", phase=Phase.PLANNING, phase_id="p1", ok=False)
        assert recorder[0].ok is False

    async def test_no_session_is_noop(self, recorder: list[Any]) -> None:
        await emit_phase_start(
            session_id=None,
            phase=Phase.PLANNING,
            phase_id="p1",
            started_at="t",
        )
        await emit_phase_end(session_id="", phase=Phase.PLANNING, phase_id="p1")
        assert recorder == []


class TestBestEffort:
    """AC-6: the emit path forced to raise must not propagate."""

    async def test_emit_start_swallows_push_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _boom(event: Any, session_id: str) -> None:
            raise RuntimeError("persist exploded")

        monkeypatch.setattr(transport_mod, "_push_event", _boom)
        # Must not raise.
        await emit_phase_start(session_id="s1", phase=Phase.PLANNING, phase_id="p1", started_at="t")
        await emit_phase_end(session_id="s1", phase=Phase.PLANNING, phase_id="p1")

    async def test_phase_span_swallows_push_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _boom(event: Any, session_id: str) -> None:
            raise RuntimeError("persist exploded")

        monkeypatch.setattr(transport_mod, "_push_event", _boom)
        ran = False
        async with phase_span(session_id="s1", phase=Phase.PLANNING) as pid:
            ran = True
            assert isinstance(pid, str)  # a session → real phase_id despite emit failure
        assert ran  # the body ran and the block exited cleanly


class TestPhaseSpan:
    async def test_pairs_start_and_end(self, recorder: list[Any]) -> None:
        async with phase_span(session_id="s1", phase=Phase.EXPANSION, detail="2 kids") as pid:
            assert isinstance(pid, str)
        assert [type(e) for e in recorder] == [PhaseStartEvent, PhaseEndEvent]
        assert recorder[0].phase_id == recorder[1].phase_id == pid
        assert recorder[0].started_at  # server timestamp present
        # ADR-0123 AC-9(b) / FRE-936: a clean exit reports ok=True, so the client
        # can tell this phase actually succeeded rather than merely "ended".
        assert recorder[1].ok is True

    async def test_end_fires_on_exception(self, recorder: list[Any]) -> None:
        with pytest.raises(ValueError):
            async with phase_span(session_id="s1", phase=Phase.PLANNING):
                raise ValueError("boom")
        assert [type(e) for e in recorder] == [PhaseStartEvent, PhaseEndEvent]
        # FRE-936: without this, a phase that raised still emits a "clean" PHASE_END
        # indistinguishable from success — the client would render a green check for
        # a phase that just failed. ok=False is what lets AC-9(b) resolve correctly.
        assert recorder[1].ok is False

    async def test_end_fires_ok_false_on_cancellation(self, recorder: list[Any]) -> None:
        """CancelledError must still propagate — ok=False is reported, not swallowed."""
        import asyncio

        with pytest.raises(asyncio.CancelledError):
            async with phase_span(session_id="s1", phase=Phase.SUB_AGENT):
                raise asyncio.CancelledError()
        assert [type(e) for e in recorder] == [PhaseStartEvent, PhaseEndEvent]
        assert recorder[1].ok is False

    async def test_noop_without_session(self, recorder: list[Any]) -> None:
        async with phase_span(session_id=None, phase=Phase.PLANNING) as pid:
            assert pid is None
        assert recorder == []
