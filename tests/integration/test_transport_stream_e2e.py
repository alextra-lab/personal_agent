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

from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from personal_agent.exceptions import UnknownSessionError
from personal_agent.service.database import AsyncSessionLocal, engine
from personal_agent.transport.agui.event_buffer import SessionEventBuffer

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_between_tests() -> AsyncGenerator[None, None]:
    """Drop pooled connections after each test so the next event loop gets fresh ones.

    ``AsyncSessionLocal`` is module-level, so its pool holds connections bound to
    whichever event loop first opened them. pytest-asyncio gives each test a new
    loop, so from the second test onward every checkout raised — and because
    ``_postgres_available`` swallows that into a skip, the suite reported green
    while silently running only its first test. Disposing between tests is what
    makes the rest of this file actually execute.
    """
    yield
    await engine.dispose()


async def _postgres_available() -> bool:
    """Return True when the test Postgres substrate (port 5433) is reachable."""
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# Fixed test user so the sessions.user_id FK (FRE-591) is satisfied without
# depending on any pre-seeded data.
_TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000591")


async def _create_session(session_id: UUID) -> None:
    """Insert a minimal session row so session_events FK constraint is satisfied."""
    async with AsyncSessionLocal() as db:
        # Seed the FK target first (sessions.user_id NOT NULL → users, FRE-591).
        await db.execute(
            text("INSERT INTO users (user_id, email) VALUES (:uid, :email) ON CONFLICT DO NOTHING"),
            {"uid": _TEST_USER_ID, "email": "transport-e2e@test.local"},
        )
        await db.execute(
            text(
                "INSERT INTO sessions (session_id, mode, user_id) "
                "VALUES (:sid, 'NORMAL', :uid) ON CONFLICT DO NOTHING"
            ),
            {"sid": session_id, "uid": _TEST_USER_ID},
        )
        await db.commit()


class TestSessionEventBuffer:
    """Real Postgres SessionEventBuffer: append, replay, oldest_available_seq."""

    @pytest.mark.asyncio
    async def test_events_stored_with_monotonic_seq(self) -> None:
        """Three appended events get distinct, monotonically increasing seq values."""
        if not await _postgres_available():
            pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")

        session_id = uuid4()
        await _create_session(session_id)

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
        await _create_session(session_id)

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
        await _create_session(session_id)

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
        await _create_session(session_id)

        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)
            seq10 = await buf.append(session_id, "TEXT_DELTA", {"data": "late"})

        # Client claims it last saw seq = (seq10 - 5), which is before the oldest event.
        stale_last_seq = seq10 - 5
        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)
            oldest = await buf.oldest_available_seq(session_id)
            # This is the condition the _sender uses to decide to send REPLAY_GAP.
            # ``+ 1`` because the client's *next* expected seq is what must still
            # be retained; ``oldest == last_seq + 1`` means nothing is missing
            # (FRE-1040 off-by-one).
            is_gap = oldest is not None and stale_last_seq + 1 < oldest

        assert is_gap, f"Expected gap: oldest={oldest}, stale_last_seq={stale_last_seq}"

        # Despite the gap, replay still returns events with seq > stale_last_seq.
        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)
            replayed = await buf.replay(session_id, after_seq=stale_last_seq)

        assert len(replayed) >= 1
        assert replayed[0]["seq"] == seq10


class TestPerSessionSequence:
    """``seq`` is allocated per session, not from a global sequence (FRE-1040).

    The client dispatches only a *contiguous* run from its stored ``ackSeq``
    (``seshat-pwa/src/lib/agui-client.ts``). A global sequence lets a second live
    conversation consume numbers inside this session's series, and the resulting
    hole is never fillable on this session's socket — the response is buffered
    forever and only a full reload recovers it.
    """

    @pytest.mark.asyncio
    async def test_interleaved_sessions_each_get_a_contiguous_series(self) -> None:
        """Appending alternately to two sessions leaves neither series with a hole."""
        if not await _postgres_available():
            pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")

        session_a = uuid4()
        session_b = uuid4()
        await _create_session(session_a)
        await _create_session(session_b)

        seqs_a: list[int] = []
        seqs_b: list[int] = []
        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)
            for i in range(4):
                seqs_a.append(await buf.append(session_a, "TEXT_DELTA", {"data": f"a{i}"}))
                seqs_b.append(await buf.append(session_b, "TEXT_DELTA", {"data": f"b{i}"}))

        assert seqs_a == list(range(seqs_a[0], seqs_a[0] + 4)), (
            f"session A's series has a hole: {seqs_a}"
        )
        assert seqs_b == list(range(seqs_b[0], seqs_b[0] + 4)), (
            f"session B's series has a hole: {seqs_b}"
        )

    @pytest.mark.asyncio
    async def test_seq_continues_from_the_stored_counter(self) -> None:
        """The next seq is ``sessions.last_event_seq + 1``, and the counter advances.

        This is the property the migration's backfill relies on: seeding every
        existing session's counter at the old global high-water mark guarantees no
        new seq can land at or below a client's already-stored ``ackSeq`` and be
        discarded as a duplicate.
        """
        if not await _postgres_available():
            pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")

        session_id = uuid4()
        await _create_session(session_id)

        high_water = 9000
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("UPDATE sessions SET last_event_seq = :hw WHERE session_id = :sid"),
                {"hw": high_water, "sid": session_id},
            )
            await db.commit()

        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)
            first = await buf.append(session_id, "TEXT_DELTA", {"data": "after-backfill"})
            second = await buf.append(session_id, "TEXT_DELTA", {"data": "next"})

        assert first == high_water + 1
        assert second == high_water + 2

        async with AsyncSessionLocal() as db:
            stored = (
                await db.execute(
                    text("SELECT last_event_seq FROM sessions WHERE session_id = :sid"),
                    {"sid": session_id},
                )
            ).scalar_one()
        assert stored == high_water + 2

    @pytest.mark.asyncio
    async def test_append_to_unknown_session_raises_and_burns_no_seq(self) -> None:
        """An append for a session that does not exist fails loudly, not silently.

        Rolling the whole allocation back is what stops a failed write from
        consuming a number and leaving a permanent hole in the series — the one
        failure mode the old ``nextval`` default could not avoid.
        """
        if not await _postgres_available():
            pytest.skip("Test Postgres (port 5433) not reachable — run make test-infra-up")

        async with AsyncSessionLocal() as db:
            buf = SessionEventBuffer(db)
            with pytest.raises(UnknownSessionError):
                await buf.append(uuid4(), "TEXT_DELTA", {"data": "orphan"})
