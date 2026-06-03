"""Tier-2 transport e2e — real Postgres SessionEventBuffer (FRE-400 / FRE-390).

Requires the isolated test substrate:

    make test-infra-up   # Postgres on :5433, ES on :9201, Neo4j on :7688

Invoked by CI's ``backend-integration`` job:

    PERSONAL_AGENT_INTEGRATION=1 pytest -m integration -k transport -v

These async tests exercise the real ``SessionEventBuffer`` dual-write path
(Postgres ``session_events`` table) to close the FRE-390 gap:
    "no test opens the real Postgres buffer and asserts on event sequences."

The ``TestClient`` / WS round-trip path is covered by the Tier-1 unit tests
(``test_ws_integration.py``) which use a ``FakeSessionEventBuffer``.
Running those two sets together gives full coverage without the event-loop
mismatch that would occur if we combined asyncpg (bound to one loop) with
Starlette's TestClient (anyio background-thread loop).
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from personal_agent.service.database import AsyncSessionLocal
from personal_agent.transport.agui.event_buffer import SessionEventBuffer

pytestmark = pytest.mark.integration


async def _postgres_available() -> bool:
    """Return True when the test Postgres substrate (port 5433) is reachable."""
    try:
        async with AsyncSessionLocal() as db:
            from sqlalchemy import text

            await db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


class TestSessionEventBuffer:
    """Real Postgres SessionEventBuffer: append, replay, oldest_available_seq."""

    @pytest.mark.asyncio
    async def test_events_stored_with_monotonic_seq(self) -> None:
        """Three appended events get distinct, monotonically increasing seq values."""
        if not await _postgres_available():
            pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")

        session_id = uuid4()

        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)
            seq1 = await buf.append(
                session_id,
                "TEXT_DELTA",
                {"type": "TEXT_DELTA", "data": {"text": "alpha"}, "session_id": str(session_id)},
            )
            seq2 = await buf.append(
                session_id,
                "TEXT_DELTA",
                {"type": "TEXT_DELTA", "data": {"text": "beta"}, "session_id": str(session_id)},
            )
            seq3 = await buf.append(
                session_id,
                "TEXT_DELTA",
                {"type": "TEXT_DELTA", "data": {"text": "gamma"}, "session_id": str(session_id)},
            )

        assert seq1 < seq2 < seq3
        assert all(isinstance(s, int) and s > 0 for s in (seq1, seq2, seq3))

    @pytest.mark.asyncio
    async def test_replay_returns_events_after_last_seq(self) -> None:
        """replay(after_seq=N) returns only events with seq > N, in insertion order."""
        if not await _postgres_available():
            pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")

        session_id = uuid4()

        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)
            seq1 = await buf.append(session_id, "TEXT_DELTA", {"data": "one"})
            seq2 = await buf.append(session_id, "TEXT_DELTA", {"data": "two"})
            seq3 = await buf.append(session_id, "TEXT_DELTA", {"data": "three"})

        # Simulate reconnect: client last saw seq1; expects seq2 and seq3 replayed.
        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)
            replayed = await buf.replay(session_id, after_seq=seq1)

        assert len(replayed) == 2
        assert replayed[0]["seq"] == seq2
        assert replayed[0]["payload"]["data"] == "two"
        assert replayed[1]["seq"] == seq3
        assert replayed[1]["payload"]["data"] == "three"

    @pytest.mark.asyncio
    async def test_oldest_available_seq_tracks_first_event(self) -> None:
        """oldest_available_seq returns the lowest seq for the session."""
        if not await _postgres_available():
            pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")

        session_id = uuid4()

        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)
            seq1 = await buf.append(session_id, "TEXT_DELTA", {"data": "x"})
            await buf.append(session_id, "TEXT_DELTA", {"data": "y"})

        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)
            oldest = await buf.oldest_available_seq(session_id)

        assert oldest == seq1

    @pytest.mark.asyncio
    async def test_replay_gap_detected_when_last_seq_is_stale(self) -> None:
        """When last_seq < oldest_available_seq, a REPLAY_GAP condition is detectable."""
        if not await _postgres_available():
            pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")

        session_id = uuid4()

        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)
            seq10 = await buf.append(session_id, "TEXT_DELTA", {"data": "late"})

        # Client claims it last saw seq = (seq10 - 5), which is before the oldest event.
        stale_last_seq = seq10 - 5
        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)
            oldest = await buf.oldest_available_seq(session_id)
            # This is the condition the _sender uses to decide to send REPLAY_GAP.
            is_gap = oldest is not None and stale_last_seq < oldest

        assert is_gap, f"Expected gap: oldest={oldest}, stale_last_seq={stale_last_seq}"

        # Despite the gap, replay still returns events with seq > stale_last_seq.
        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)
            replayed = await buf.replay(session_id, after_seq=stale_last_seq)

        assert len(replayed) >= 1
        assert replayed[0]["seq"] == seq10
