"""Tests for the main_inference silence detector (FRE-1117).

Ground truth established against live Postgres/ES before writing this ticket
(see docs/superpowers/plans/2026-08-01-fre-1117-main-inference-silence-detector.md):
``main_inference`` (the primary role's budget lane) can legitimately show zero
reservations for days at a time whenever primary is selected local — this is
the *normal*, current operating state, not a bug. The detector exists to flag
the one case that genuinely is worth a human's attention: a session had
primary-role turns AND that session's own model selection was cloud, yet no
``main_inference`` reservation exists for it. Every "no finding" test here is
a regression guard against false-positiving on the correct, current state.

Real-DB tests against the test-stack Postgres (:5433 — FRE-375 isolation),
mirroring ``test_session_model_selection_repository.py``.
"""

from __future__ import annotations

import asyncio
import socket
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from personal_agent.config.model_loader import load_model_config
from personal_agent.cost_gate.silence_monitor import find_silent_main_inference_day
from personal_agent.service.database import AsyncSessionLocal, engine


def _postgres_available() -> bool:
    try:
        with socket.create_connection(("localhost", 5433), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture(autouse=True)
def _skip_without_test_postgres():
    if not _postgres_available():
        pytest.skip("test Postgres :5433 unreachable — run make test-infra-up")


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_per_test():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_session(db) -> tuple:
    """Create a bare user + session row, returns (user_id, session_id)."""
    user_id = uuid4()
    session_id = uuid4()
    await db.execute(
        text("INSERT INTO users (user_id, email) VALUES (:uid, :email)"),
        {"uid": user_id, "email": f"fre1117-{user_id}@test.invalid"},
    )
    await db.execute(
        text(
            "INSERT INTO sessions (session_id, user_id, created_at, last_active_at)"
            " VALUES (:sid, :uid, :now, :now)"
        ),
        {"sid": session_id, "uid": user_id, "now": datetime.now(timezone.utc)},
    )
    await db.commit()
    return user_id, session_id


async def _seed_route_trace(db, session_id, *, when: datetime) -> None:
    await db.execute(
        text(
            "INSERT INTO route_traces"
            " (trace_id, session_id, created_at, model_role, orchestration_event)"
            " VALUES (:tid, :sid, :when, 'primary', 'turn_completed')"
        ),
        {"tid": uuid4(), "sid": session_id, "when": when},
    )
    await db.commit()


async def _seed_selection(db, session_id, *, deployment_key: str, updated_at: datetime) -> None:
    await db.execute(
        text(
            "INSERT INTO session_model_selections"
            " (session_id, role, deployment_key, created_at, updated_at)"
            " VALUES (:sid, 'primary', :key, :when, :when)"
        ),
        {"sid": session_id, "key": deployment_key, "when": updated_at},
    )
    await db.commit()


async def _seed_reservation(db, session_id, *, day: date, amount: Decimal) -> int:
    """Insert a committed main_inference reservation for session_id on day.

    Bypasses ``CostGate.reserve()`` (which pins to a shared prod-mirroring
    counter row) in favour of a self-contained counter + reservation pair
    this test owns end to end, so it can never collide with another test's
    or production's ``main_inference`` counter.
    """
    window_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    counter_id = (
        await db.execute(
            text(
                "INSERT INTO budget_counters"
                " (time_window, role, window_start, running_total)"
                " VALUES ('daily', 'main_inference', :ws, :amt)"
                " RETURNING id"
            ),
            {"ws": window_start, "amt": amount},
        )
    ).scalar_one()
    await db.execute(
        text(
            "INSERT INTO budget_reservations"
            " (counter_id, role, amount_usd, actual_cost_usd, status,"
            "  created_at, expires_at, settled_at, session_id)"
            " VALUES (:cid, 'main_inference', :amt, :amt, 'committed',"
            "  :when, :when, :when, :sid)"
        ),
        {"cid": counter_id, "amt": amount, "when": window_start, "sid": session_id},
    )
    await db.commit()
    return counter_id


async def _cleanup(db, *, user_ids, session_ids, counter_ids) -> None:
    if counter_ids:
        await db.execute(
            text("DELETE FROM budget_reservations WHERE counter_id = ANY(:cids)"),
            {"cids": list(counter_ids)},
        )
        await db.execute(
            text("DELETE FROM budget_counters WHERE id = ANY(:cids)"), {"cids": list(counter_ids)}
        )
    if session_ids:
        await db.execute(
            text("DELETE FROM route_traces WHERE session_id = ANY(:sids)"),
            {"sids": list(session_ids)},
        )
        await db.execute(
            text("DELETE FROM sessions WHERE session_id = ANY(:sids)"), {"sids": list(session_ids)}
        )
    if user_ids:
        await db.execute(
            text("DELETE FROM users WHERE user_id = ANY(:uids)"), {"uids": list(user_ids)}
        )
    await db.commit()


TEST_DAY = date(2020, 6, 15)
DAY_START = datetime(TEST_DAY.year, TEST_DAY.month, TEST_DAY.day, tzinfo=timezone.utc)
DAY_MID = DAY_START + timedelta(hours=12)
DAY_END = DAY_START + timedelta(days=1)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_finding_when_no_primary_turns():
    """No route_traces at all for the day -> correctly quiet, no finding."""
    model_config = load_model_config()
    async with AsyncSessionLocal() as db:
        finding = await find_silent_main_inference_day(
            db, model_config, day=TEST_DAY + timedelta(days=100)
        )
        assert finding is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_finding_when_selection_is_local():
    """Primary turns + LOCAL selection + zero reservations -> no finding.

    This is today's real, correct operating state (primary pinned to the
    local model) — the detector must not flag it.
    """
    model_config = load_model_config()
    async with AsyncSessionLocal() as db:
        user_id, session_id = await _seed_session(db)
        try:
            await _seed_route_trace(db, session_id, when=DAY_MID)
            await _seed_selection(
                db, session_id, deployment_key="qwen3.6-35b-thinking", updated_at=DAY_START
            )
            finding = await find_silent_main_inference_day(db, model_config, day=TEST_DAY)
            assert finding is None
        finally:
            await _cleanup(db, user_ids=[user_id], session_ids=[session_id], counter_ids=[])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_selection_row_defaults_to_local_no_finding():
    """No session_model_selections row at all -> binding default (local) -> no finding."""
    model_config = load_model_config()
    async with AsyncSessionLocal() as db:
        user_id, session_id = await _seed_session(db)
        try:
            await _seed_route_trace(db, session_id, when=DAY_MID)
            finding = await find_silent_main_inference_day(db, model_config, day=TEST_DAY)
            assert finding is None
        finally:
            await _cleanup(db, user_ids=[user_id], session_ids=[session_id], counter_ids=[])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_finding_when_selection_is_cloud_and_silent():
    """Primary turns + CLOUD selection + zero reservations -> flagged."""
    model_config = load_model_config()
    async with AsyncSessionLocal() as db:
        user_id, session_id = await _seed_session(db)
        try:
            await _seed_route_trace(db, session_id, when=DAY_MID)
            await _seed_selection(
                db, session_id, deployment_key="claude_sonnet", updated_at=DAY_START
            )
            finding = await find_silent_main_inference_day(db, model_config, day=TEST_DAY)
            assert finding is not None
            assert session_id in finding.cloud_selected_sessions
        finally:
            await _cleanup(db, user_ids=[user_id], session_ids=[session_id], counter_ids=[])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_finding_when_reservations_exist():
    """CLOUD selection but a committed reservation already exists -> no finding."""
    model_config = load_model_config()
    async with AsyncSessionLocal() as db:
        user_id, session_id = await _seed_session(db)
        counter_id = None
        try:
            await _seed_route_trace(db, session_id, when=DAY_MID)
            await _seed_selection(
                db, session_id, deployment_key="claude_sonnet", updated_at=DAY_START
            )
            counter_id = await _seed_reservation(
                db, session_id, day=TEST_DAY, amount=Decimal("0.05")
            )
            finding = await find_silent_main_inference_day(db, model_config, day=TEST_DAY)
            assert finding is None
        finally:
            await _cleanup(
                db,
                user_ids=[user_id],
                session_ids=[session_id],
                counter_ids=[counter_id] if counter_id else [],
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_other_session_reservation_does_not_suppress_finding():
    """Regression test for the codex-review finding.

    main_inference is a shared lane (primary, sub_agent, compressor,
    vision, ...). A committed reservation from a DIFFERENT session must not
    clear a genuinely silent session's finding — the check has to be
    per-session, not a lane-wide count.
    """
    model_config = load_model_config()
    async with AsyncSessionLocal() as db:
        user_a, session_a = await _seed_session(db)
        user_b, session_b = await _seed_session(db)
        counter_id = None
        try:
            # Session A: cloud-selected primary turn, genuinely silent.
            await _seed_route_trace(db, session_a, when=DAY_MID)
            await _seed_selection(
                db, session_a, deployment_key="claude_sonnet", updated_at=DAY_START
            )
            # Session B: unrelated activity that DOES book to the shared
            # main_inference lane the same day (e.g. a vision escalation).
            counter_id = await _seed_reservation(
                db, session_b, day=TEST_DAY, amount=Decimal("0.02")
            )

            finding = await find_silent_main_inference_day(db, model_config, day=TEST_DAY)
            assert finding is not None
            assert session_a in finding.cloud_selected_sessions
            assert session_b not in finding.cloud_selected_sessions
        finally:
            await _cleanup(
                db,
                user_ids=[user_a, user_b],
                session_ids=[session_a, session_b],
                counter_ids=[counter_id] if counter_id else [],
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_selection_changed_after_day_is_skipped():
    """A selection changed after the audited day must be skipped, not scored.

    ``updated_at`` after the day being checked means
    ``session_model_selections`` (no history) can't attribute it to that
    day — the session must be skipped, not flagged, not cleared.
    """
    model_config = load_model_config()
    async with AsyncSessionLocal() as db:
        user_id, session_id = await _seed_session(db)
        try:
            await _seed_route_trace(db, session_id, when=DAY_MID)
            # Selection set the day AFTER the audited day.
            await _seed_selection(
                db,
                session_id,
                deployment_key="claude_sonnet",
                updated_at=DAY_END + timedelta(hours=1),
            )
            finding = await find_silent_main_inference_day(db, model_config, day=TEST_DAY)
            assert finding is None
        finally:
            await _cleanup(db, user_ids=[user_id], session_ids=[session_id], counter_ids=[])


# ---------------------------------------------------------------------------
# Unit test — no live DB (AC-4: the loop must survive a check failure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_survives_check_failure_and_keeps_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A query failure inside the loop is logged and polling continues.

    Mirrors the reaper/snapshotter's own untested-loop convention (the
    wrapper itself has no interesting logic) EXCEPT this loop has retry
    semantics worth pinning directly: ``last_checked_day`` must not advance
    on a failed check, so the same day is retried rather than silently
    skipped forever. Drives ``find_silent_main_inference_day`` to always
    raise and asserts the task survives several poll intervals without
    propagating that error — the exact promise AC-4 makes.
    """
    import personal_agent.cost_gate.silence_monitor as silence_monitor_mod
    import personal_agent.service.database as database_mod

    call_count = 0

    async def _always_fails(db: object, model_config: object, *, day: object) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("simulated query failure")

    class _FakeSession:
        async def __aenter__(self) -> "_FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(silence_monitor_mod, "find_silent_main_inference_day", _always_fails)
    monkeypatch.setattr(database_mod, "AsyncSessionLocal", lambda: _FakeSession())

    model_config = load_model_config()
    task = asyncio.create_task(
        silence_monitor_mod.run_silence_monitor(model_config, interval_seconds=0.01)
    )
    await asyncio.sleep(0.05)  # let several poll iterations run
    task.cancel()
    await task  # must not raise -- neither the injected RuntimeError nor CancelledError

    assert call_count >= 2, "expected the loop to retry the failed day, not stop after the first"
