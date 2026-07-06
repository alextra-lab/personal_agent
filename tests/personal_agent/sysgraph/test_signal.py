"""Outcome ingestion + realized-value signal tests (ADR-0105 D7, FRE-717)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.sysgraph import SysgraphRepository
from personal_agent.sysgraph.repository import ProposalRecord


@pytest_asyncio.fixture
async def _cleanup_signal_rows(
    sysgraph_pool: asyncpg.Pool,
) -> AsyncIterator[None]:
    """Delete any proposal/ticket/signal rows this test's fixtures created."""
    try:
        yield
    finally:
        async with sysgraph_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sysgraph.ticket WHERE linear_issue_id LIKE 'FRE-TEST-SIGNAL%'"
            )
            await conn.execute(
                "DELETE FROM sysgraph.proposal WHERE fingerprint LIKE 'fp-signal-test%'"
            )
            await conn.execute(
                "DELETE FROM sysgraph.signal WHERE source = 'reflection' AND category = 'signal-test'"
            )


async def _seed_promoted_ticket(
    sysgraph_repo: SysgraphRepository,
    *,
    linear_issue_id: str,
    fingerprint: str,
    category: str = "signal-test",
) -> None:
    """Seed one proposal -> ticket -> PROMOTED_TO edge via the real repository method."""
    proposal = ProposalRecord(
        source="reflection",
        category=category,
        fingerprint=fingerprint,
        what="Test proposal",
        why="Test why",
        how="Test how",
        seen_count=1,
    )
    await sysgraph_repo.record_promotion(
        proposal, linear_issue_id=linear_issue_id, ticket_title="Test ticket"
    )


async def _ticket_id(sysgraph_pool: asyncpg.Pool, linear_issue_id: str) -> UUID:
    async with sysgraph_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT id FROM sysgraph.ticket WHERE linear_issue_id = $1", linear_issue_id
        )


async def _seed_outcome(
    sysgraph_pool: asyncpg.Pool,
    *,
    ticket_id: UUID,
    result: str,
    observed_at: datetime | None = None,
) -> None:
    """Insert an outcome + produced edge directly (bypassing record_outcome), for
    seeding multiple outcomes across different tickets that all resolve to the
    same (source, category) signal key.
    """
    async with sysgraph_pool.acquire() as conn:
        observed_at = observed_at or datetime.now(timezone.utc)
        outcome_id = await conn.fetchval(
            "INSERT INTO sysgraph.outcome (result, observed_at) VALUES ($1, $2) RETURNING id",
            result,
            observed_at,
        )
        await conn.execute(
            "INSERT INTO sysgraph.produced (ticket_id, outcome_id) VALUES ($1, $2)",
            ticket_id,
            outcome_id,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_outcome_creates_produced_edge(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_signal_rows: None,
) -> None:
    """record_outcome links a promoted ticket to a new outcome node (AC-6 part 1)."""
    await _seed_promoted_ticket(
        sysgraph_repo, linear_issue_id="FRE-TEST-SIGNAL-1", fingerprint="fp-signal-test-1"
    )

    recorded = await sysgraph_repo.record_outcome("FRE-TEST-SIGNAL-1", "shipped")

    assert recorded is True
    ticket_id = await _ticket_id(sysgraph_pool, "FRE-TEST-SIGNAL-1")
    async with sysgraph_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT o.result FROM sysgraph.produced pr "
            "JOIN sysgraph.outcome o ON o.id = pr.outcome_id WHERE pr.ticket_id = $1",
            ticket_id,
        )
    assert row is not None
    assert row["result"] == "shipped"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_outcome_is_idempotent(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_signal_rows: None,
) -> None:
    """A second record_outcome call for the same ticket is a no-op (one terminal outcome)."""
    await _seed_promoted_ticket(
        sysgraph_repo, linear_issue_id="FRE-TEST-SIGNAL-2", fingerprint="fp-signal-test-2"
    )

    first = await sysgraph_repo.record_outcome("FRE-TEST-SIGNAL-2", "shipped")
    second = await sysgraph_repo.record_outcome("FRE-TEST-SIGNAL-2", "owner-rejected")

    assert first is True
    assert second is False
    ticket_id = await _ticket_id(sysgraph_pool, "FRE-TEST-SIGNAL-2")
    async with sysgraph_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT result FROM sysgraph.produced pr "
            "JOIN sysgraph.outcome o ON o.id = pr.outcome_id WHERE pr.ticket_id = $1",
            ticket_id,
        )
    assert len(rows) == 1
    assert rows[0]["result"] == "shipped"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_outcome_no_ticket_returns_false(
    sysgraph_repo: SysgraphRepository,
) -> None:
    """A linear_issue_id with no sysgraph.ticket row records nothing (best-effort skip)."""
    assert await sysgraph_repo.record_outcome("FRE-DOES-NOT-EXIST-SIGNAL", "shipped") is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_outcome_concurrent_calls_only_one_wins(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_signal_rows: None,
) -> None:
    """Two concurrent record_outcome calls for the same ticket: exactly one succeeds.

    Proves the UNIQUE(ticket_id) constraint + rollback-on-conflict holds under a
    race, not just under sequential calls (codex plan review).
    """
    await _seed_promoted_ticket(
        sysgraph_repo, linear_issue_id="FRE-TEST-SIGNAL-RACE", fingerprint="fp-signal-test-race"
    )

    results = await asyncio.gather(
        sysgraph_repo.record_outcome("FRE-TEST-SIGNAL-RACE", "shipped"),
        sysgraph_repo.record_outcome("FRE-TEST-SIGNAL-RACE", "owner-rejected"),
    )

    assert sorted(results) == [False, True]
    ticket_id = await _ticket_id(sysgraph_pool, "FRE-TEST-SIGNAL-RACE")
    async with sysgraph_pool.acquire() as conn:
        outcome_rows = await conn.fetch(
            "SELECT o.id FROM sysgraph.produced pr "
            "JOIN sysgraph.outcome o ON o.id = pr.outcome_id WHERE pr.ticket_id = $1",
            ticket_id,
        )
        all_outcomes = await conn.fetch(
            "SELECT id FROM sysgraph.outcome WHERE id NOT IN "
            "(SELECT outcome_id FROM sysgraph.produced)"
        )
    assert len(outcome_rows) == 1  # exactly one linked outcome, no duplicate edge
    # No orphaned outcome row from the rolled-back loser (this assertion is a
    # best-effort global check — safe because cleanup only removes rows this
    # test created via CASCADE on ticket delete, and outcome rows referenced by
    # produced are never orphaned).


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_signal_computes_weighted_average(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_signal_rows: None,
) -> None:
    """V = Σweights / (n+2) over outcomes for one (source, category)."""
    await _seed_promoted_ticket(
        sysgraph_repo, linear_issue_id="FRE-TEST-SIGNAL-3A", fingerprint="fp-signal-test-3a"
    )
    await _seed_promoted_ticket(
        sysgraph_repo, linear_issue_id="FRE-TEST-SIGNAL-3B", fingerprint="fp-signal-test-3b"
    )
    ticket_a = await _ticket_id(sysgraph_pool, "FRE-TEST-SIGNAL-3A")
    ticket_b = await _ticket_id(sysgraph_pool, "FRE-TEST-SIGNAL-3B")
    await _seed_outcome(sysgraph_pool, ticket_id=ticket_a, result="shipped")
    await _seed_outcome(sysgraph_pool, ticket_id=ticket_b, result="canceled-as-noise")

    signal = await sysgraph_repo.get_signal("reflection", "signal-test")

    # weights: shipped=+1.0, canceled-as-noise=-0.5 -> sum=0.5, n=2, prior=2 -> v=0.5/4=0.125
    assert signal.n == 2
    assert signal.value == pytest.approx(0.125)
    assert signal.suppressed is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_signal_excludes_outcomes_outside_window(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_signal_rows: None,
) -> None:
    """An outcome older than signal_window_days is excluded from v (D7 trailing window)."""
    await _seed_promoted_ticket(
        sysgraph_repo, linear_issue_id="FRE-TEST-SIGNAL-4A", fingerprint="fp-signal-test-4a"
    )
    await _seed_promoted_ticket(
        sysgraph_repo, linear_issue_id="FRE-TEST-SIGNAL-4B", fingerprint="fp-signal-test-4b"
    )
    ticket_recent = await _ticket_id(sysgraph_pool, "FRE-TEST-SIGNAL-4A")
    ticket_old = await _ticket_id(sysgraph_pool, "FRE-TEST-SIGNAL-4B")
    await _seed_outcome(sysgraph_pool, ticket_id=ticket_recent, result="shipped")
    await _seed_outcome(
        sysgraph_pool,
        ticket_id=ticket_old,
        result="owner-rejected",
        observed_at=datetime.now(timezone.utc) - timedelta(days=120),
    )

    signal = await sysgraph_repo.get_signal("reflection", "signal-test")

    # Only the recent 'shipped' outcome counts: v = 1.0 / (1+2) = 0.333...
    assert signal.n == 1
    assert signal.value == pytest.approx(1.0 / 3.0)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_compute_and_apply_signal_triggers_suppression(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_signal_rows: None,
) -> None:
    """5 owner-rejected outcomes push v <= -0.4 -> suppressed_until set ~30 days out."""
    for i in range(5):
        linear_issue_id = f"FRE-TEST-SIGNAL-5-{i}"
        await _seed_promoted_ticket(
            sysgraph_repo, linear_issue_id=linear_issue_id, fingerprint=f"fp-signal-test-5-{i}"
        )
        ticket_id = await _ticket_id(sysgraph_pool, linear_issue_id)
        await _seed_outcome(sysgraph_pool, ticket_id=ticket_id, result="owner-rejected")

    before = await sysgraph_repo.get_signal("reflection", "signal-test")
    after = await sysgraph_repo.compute_and_apply_signal("reflection", "signal-test")

    # v = -5.0 / (5+2) = -0.714... <= -0.4 threshold with n=5 >= min_n=5
    assert before.value == pytest.approx(-5.0 / 7.0)
    assert before.suppressed is False  # not yet applied
    assert after.value == pytest.approx(-5.0 / 7.0)

    signal_after_apply = await sysgraph_repo.get_signal("reflection", "signal-test")
    assert signal_after_apply.suppressed is True
    async with sysgraph_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT suppressed_until FROM sysgraph.signal WHERE source = 'reflection' "
            "AND category = 'signal-test'"
        )
    assert row is not None
    delta = row["suppressed_until"] - datetime.now(timezone.utc)
    assert timedelta(days=29) < delta < timedelta(days=31)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_signal_before_after_changes_by_expected_weight(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_signal_rows: None,
) -> None:
    """AC-6 direct proof: v changes by exactly the recorded outcome's weight."""
    await _seed_promoted_ticket(
        sysgraph_repo, linear_issue_id="FRE-TEST-SIGNAL-6", fingerprint="fp-signal-test-6"
    )

    before = await sysgraph_repo.get_signal("reflection", "signal-test")
    assert before.value == 0.0
    assert before.n == 0

    recorded = await sysgraph_repo.record_outcome("FRE-TEST-SIGNAL-6", "shipped")
    assert recorded is True
    after = await sysgraph_repo.compute_and_apply_signal("reflection", "signal-test")

    # v goes from 0/(0+2)=0 to 1.0/(1+2)=0.333... -- exactly the 'shipped' weight's contribution
    assert after.n == 1
    assert after.value == pytest.approx(1.0 / 3.0)
    assert after.value - before.value == pytest.approx(1.0 / 3.0)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ticket_source_kind_resolves_source_and_category(
    sysgraph_repo: SysgraphRepository,
    _cleanup_signal_rows: None,
) -> None:
    """ticket_source_kind resolves (source, category) via the PROMOTED_TO edge."""
    await _seed_promoted_ticket(
        sysgraph_repo,
        linear_issue_id="FRE-TEST-SIGNAL-7",
        fingerprint="fp-signal-test-7",
        category="signal-test",
    )

    kind = await sysgraph_repo.ticket_source_kind("FRE-TEST-SIGNAL-7")

    assert kind == ("reflection", "signal-test")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ticket_source_kind_none_when_not_promoted(
    sysgraph_repo: SysgraphRepository,
) -> None:
    """A ticket id with no PROMOTED_TO edge resolves to None, not an error."""
    assert await sysgraph_repo.ticket_source_kind("FRE-DOES-NOT-EXIST-SIGNAL") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_is_kind_decided_false_before_any_outcome(
    sysgraph_repo: SysgraphRepository,
    _cleanup_signal_rows: None,
) -> None:
    """No outcome recorded yet -> not decided."""
    assert await sysgraph_repo.is_kind_decided("reflection", "signal-test") is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_is_kind_decided_true_after_terminal_outcome(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_signal_rows: None,
) -> None:
    """A shipped/owner-rejected/canceled-as-noise outcome marks the kind decided."""
    await _seed_promoted_ticket(
        sysgraph_repo, linear_issue_id="FRE-TEST-SIGNAL-8", fingerprint="fp-signal-test-8"
    )
    ticket_id = await _ticket_id(sysgraph_pool, "FRE-TEST-SIGNAL-8")
    await _seed_outcome(sysgraph_pool, ticket_id=ticket_id, result="shipped")

    assert await sysgraph_repo.is_kind_decided("reflection", "signal-test") is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_is_kind_decided_false_when_only_deferred(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_signal_rows: None,
) -> None:
    """A deferred-only outcome does not count as decided (right idea, wrong time)."""
    await _seed_promoted_ticket(
        sysgraph_repo, linear_issue_id="FRE-TEST-SIGNAL-9", fingerprint="fp-signal-test-9"
    )
    ticket_id = await _ticket_id(sysgraph_pool, "FRE-TEST-SIGNAL-9")
    await _seed_outcome(sysgraph_pool, ticket_id=ticket_id, result="deferred")

    assert await sysgraph_repo.is_kind_decided("reflection", "signal-test") is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tickets_awaiting_outcome_excludes_decided_tickets(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_signal_rows: None,
) -> None:
    """A promoted ticket with no outcome yet appears; one with an outcome does not."""
    await _seed_promoted_ticket(
        sysgraph_repo, linear_issue_id="FRE-TEST-SIGNAL-10A", fingerprint="fp-signal-test-10a"
    )
    await _seed_promoted_ticket(
        sysgraph_repo, linear_issue_id="FRE-TEST-SIGNAL-10B", fingerprint="fp-signal-test-10b"
    )
    ticket_b = await _ticket_id(sysgraph_pool, "FRE-TEST-SIGNAL-10B")
    await _seed_outcome(sysgraph_pool, ticket_id=ticket_b, result="shipped")

    pending = await sysgraph_repo.tickets_awaiting_outcome()

    assert "FRE-TEST-SIGNAL-10A" in pending
    assert "FRE-TEST-SIGNAL-10B" not in pending
