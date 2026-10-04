"""FRE-1543 — the durable status snapshot and the in-flight turn registry.

The snapshot replaces the in-memory-only status bar after a reload: it must survive a gateway
restart (Postgres, not the projector's process-local session lane — FRE-1401), keep the last
ctx reading when a new reading has no resolved ceiling (the PWA ``mergeTurnStatus`` rule), and
reset only the tools lane at a send (FRE-1538).
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from personal_agent.transport.agui.turn_state import (
    TurnStatusStore,
    merge_turn_status,
    turn_ended,
    turn_start_seq,
    turn_started,
)


def _reading(*, ctx: int, ctx_max: int | None, tools: tuple[int, int] | None) -> dict[str, object]:
    return {
        "context_tokens": ctx,
        "session_context_tokens": ctx,
        "context_max": ctx_max,
        "tool_iteration": tools[0] if tools else None,
        "tool_iteration_max": tools[1] if tools else None,
        "turn_cost_usd": 0.01,
        "trace_id": "t1",
    }


# ── merge rule (pure) ──────────────────────────────────────────────────────────


def test_merge_takes_the_new_reading_when_it_has_a_ctx_reading() -> None:
    prev = _reading(ctx=12000, ctx_max=128000, tools=(3, 6))
    new = _reading(ctx=4000, ctx_max=128000, tools=(1, 6))

    assert merge_turn_status(prev, new) == new


def test_merge_carries_ctx_over_a_reading_with_no_resolved_ceiling() -> None:
    prev = _reading(ctx=12000, ctx_max=128000, tools=(3, 6))
    new = _reading(ctx=0, ctx_max=None, tools=(1, 25))

    merged = merge_turn_status(prev, new)

    assert merged["context_max"] == 128000
    assert merged["session_context_tokens"] == 12000
    assert merged["context_tokens"] == 12000
    # The tools lane always comes from the new reading.
    assert (merged["tool_iteration"], merged["tool_iteration_max"]) == (1, 25)


def test_merge_with_no_previous_reading_never_invents_a_ceiling() -> None:
    new = _reading(ctx=0, ctx_max=None, tools=None)

    assert merge_turn_status(None, new) == new


# ── in-flight registry ─────────────────────────────────────────────────────────


def test_registry_reports_the_earliest_start_of_overlapping_turns() -> None:
    sid = str(uuid4())
    assert turn_start_seq(sid) is None

    turn_started(sid, 40)
    turn_started(sid, 55)
    assert turn_start_seq(sid) == 40

    turn_ended(sid)
    assert turn_start_seq(sid) == 40  # one turn still runs
    turn_ended(sid)
    assert turn_start_seq(sid) is None
    turn_ended(sid)  # an extra end is a no-op
    assert turn_start_seq(sid) is None


# ── Postgres store (:5433) ─────────────────────────────────────────────────────


def _postgres_available() -> bool:
    try:
        with socket.create_connection(("localhost", 5433), timeout=2):
            return True
    except OSError:
        return False


@pytest_asyncio.fixture
async def seeded_session() -> AsyncIterator[UUID]:
    if not _postgres_available():
        pytest.skip("Test Postgres (:5433) not reachable — run make test-infra-up")

    from sqlalchemy import text

    from personal_agent.service.database import AsyncSessionLocal, engine

    # An earlier test may have left pooled connections bound to its own (now closed) event
    # loop. Drop that pool without touching those connections; this test opens fresh ones.
    await engine.dispose(close=False)
    user_id = uuid4()
    session_id = uuid4()
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("INSERT INTO users (user_id, email) VALUES (:uid, :email) ON CONFLICT DO NOTHING"),
            {"uid": user_id, "email": f"fre1543-{user_id}@test.invalid"},
        )
        await db.execute(
            text(
                "INSERT INTO sessions (session_id, user_id, mode, channel, execution_profile,"
                " metadata) VALUES (:sid, :uid, 'NORMAL', 'CHAT', 'local',"
                " '{\"other_key\": 1}'::jsonb)"
            ),
            {"sid": session_id, "uid": user_id},
        )
        await db.commit()
    try:
        yield session_id
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("DELETE FROM sessions WHERE session_id = :sid"), {"sid": session_id}
            )
            await db.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})
            await db.commit()
        # Each test runs on its own event loop; a pooled connection bound to this loop
        # must not leak into the next test.
        await engine.dispose()


@pytest.mark.asyncio
async def test_store_survives_a_separate_connection_and_merges(seeded_session: UUID) -> None:
    """AC-3 source: saved on one connection, read back on another (a restarted gateway)."""
    from sqlalchemy import text

    from personal_agent.service.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        assert await TurnStatusStore(db).load(seeded_session) is None
        await TurnStatusStore(db).save(
            seeded_session, _reading(ctx=19000, ctx_max=128000, tools=(1, 25))
        )
    async with AsyncSessionLocal() as db:
        await TurnStatusStore(db).save(seeded_session, _reading(ctx=0, ctx_max=None, tools=(2, 25)))

    async with AsyncSessionLocal() as db:
        loaded = await TurnStatusStore(db).load(seeded_session)
        other = (
            await db.execute(
                text("SELECT metadata -> 'other_key' FROM sessions WHERE session_id = :sid"),
                {"sid": seeded_session},
            )
        ).scalar_one()

    assert loaded is not None
    assert (loaded["tool_iteration"], loaded["tool_iteration_max"]) == (2, 25)
    assert (loaded["session_context_tokens"], loaded["context_max"]) == (19000, 128000)
    assert other == 1  # a key-level write never clobbers another metadata key


@pytest.mark.asyncio
async def test_clear_tools_resets_only_the_tools_lane(seeded_session: UUID) -> None:
    from personal_agent.service.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await TurnStatusStore(db).save(
            seeded_session, _reading(ctx=19000, ctx_max=128000, tools=(2, 25))
        )
        await TurnStatusStore(db).clear_tools(seeded_session)
        loaded = await TurnStatusStore(db).load(seeded_session)

    assert loaded is not None
    assert loaded["tool_iteration"] is None
    assert loaded["tool_iteration_max"] is None
    assert (loaded["session_context_tokens"], loaded["context_max"]) == (19000, 128000)


@pytest.mark.asyncio
async def test_clear_tools_with_no_stored_reading_stores_nothing(seeded_session: UUID) -> None:
    from personal_agent.service.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await TurnStatusStore(db).clear_tools(seeded_session)
        assert await TurnStatusStore(db).load(seeded_session) is None


@pytest.mark.asyncio
async def test_save_for_an_unknown_session_is_a_no_op() -> None:
    if not _postgres_available():
        pytest.skip("Test Postgres (:5433) not reachable — run make test-infra-up")
    from personal_agent.service.database import AsyncSessionLocal, engine

    await engine.dispose(close=False)
    try:
        async with AsyncSessionLocal() as db:
            await TurnStatusStore(db).save(uuid4(), _reading(ctx=1, ctx_max=2, tools=None))
            assert await TurnStatusStore(db).load(uuid4()) is None
    finally:
        await engine.dispose()
