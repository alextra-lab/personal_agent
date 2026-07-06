"""Generation-time read-before-emit tests (ADR-0105 D9/D10, FRE-721/T7)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.sysgraph import SysgraphRepository
from personal_agent.sysgraph.repository import ProposalRecord


@pytest_asyncio.fixture
async def _cleanup_rbe_rows(sysgraph_pool: asyncpg.Pool) -> AsyncIterator[None]:
    """Delete any rows this test's fixed fingerprints/source/category created."""
    try:
        yield
    finally:
        async with sysgraph_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sysgraph.produced WHERE ticket_id IN "
                "(SELECT id FROM sysgraph.ticket WHERE linear_issue_id = 'FRE-TEST-RBE-DECIDED')"
            )
            await conn.execute(
                "DELETE FROM sysgraph.promoted_to WHERE ticket_id IN "
                "(SELECT id FROM sysgraph.ticket WHERE linear_issue_id = 'FRE-TEST-RBE-DECIDED')"
            )
            await conn.execute(
                "DELETE FROM sysgraph.ticket WHERE linear_issue_id = 'FRE-TEST-RBE-DECIDED'"
            )
            await conn.execute(
                "DELETE FROM sysgraph.proposal WHERE fingerprint LIKE 'fp-rbe-test-%'"
            )


async def _seed_decided_outcome(
    pool: asyncpg.Pool, *, source: str, category: str, result: str = "owner-rejected"
) -> None:
    """Seed a full proposal -> ticket -> outcome chain so (source, category) is decided."""
    async with pool.acquire() as conn:
        proposal_id = await conn.fetchval(
            "INSERT INTO sysgraph.proposal (source, category, fingerprint, what, seen_count) "
            "VALUES ($1, $2, 'fp-rbe-test-decided-seed', 'seed', 1) RETURNING id",
            source,
            category,
        )
        ticket_id = await conn.fetchval(
            "INSERT INTO sysgraph.ticket (linear_issue_id, title) "
            "VALUES ('FRE-TEST-RBE-DECIDED', 'seed ticket') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO sysgraph.promoted_to (proposal_id, ticket_id) VALUES ($1, $2)",
            proposal_id,
            ticket_id,
        )
        outcome_id = await conn.fetchval(
            "INSERT INTO sysgraph.outcome (result) VALUES ($1) RETURNING id", result
        )
        await conn.execute(
            "INSERT INTO sysgraph.produced (ticket_id, outcome_id) VALUES ($1, $2)",
            ticket_id,
            outcome_id,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_before_emit_decided_skip_no_new_row(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_rbe_rows: None,
) -> None:
    """AC-9: an equivalent already-decided kind produces no new proposal row."""
    await _seed_decided_outcome(sysgraph_pool, source="reflection", category="rbe-decided-cat")

    result = await sysgraph_repo.read_before_emit(
        "reflection",
        "rbe-decided-cat",
        "orchestrator",
        ProposalRecord(
            source="reflection",
            category="rbe-decided-cat",
            fingerprint="fp-rbe-test-would-be-new",
            what="new idea text",
            why=None,
            how=None,
            seen_count=1,
            scope="orchestrator",
        ),
    )

    assert result.decision == "decided_skip"
    assert result.proposal_id is None
    async with sysgraph_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM sysgraph.proposal WHERE fingerprint = 'fp-rbe-test-would-be-new'"
        )
    assert count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_before_emit_reinforces_awaiting_equivalent(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_rbe_rows: None,
) -> None:
    """AC-9: an equivalent still-awaiting proposal reinforces (seen_count++), no new row."""
    async with sysgraph_pool.acquire() as conn:
        existing_id = await conn.fetchval(
            "INSERT INTO sysgraph.proposal (source, category, fingerprint, what, seen_count, scope) "
            "VALUES ('reflection', 'rbe-awaiting-cat', 'fp-rbe-test-awaiting-seed', 'seed', 3, "
            "'orchestrator') RETURNING id"
        )

    result = await sysgraph_repo.read_before_emit(
        "reflection",
        "rbe-awaiting-cat",
        "orchestrator",
        ProposalRecord(
            source="reflection",
            category="rbe-awaiting-cat",
            fingerprint="fp-rbe-test-would-be-new-2",
            what="new phrasing of the same idea",
            why=None,
            how=None,
            seen_count=1,
            scope="orchestrator",
        ),
    )

    assert result.decision == "reinforced"
    assert result.proposal_id == existing_id
    async with sysgraph_pool.acquire() as conn:
        seen_count = await conn.fetchval(
            "SELECT seen_count FROM sysgraph.proposal WHERE id = $1", existing_id
        )
        dup_count = await conn.fetchval(
            "SELECT COUNT(*) FROM sysgraph.proposal WHERE fingerprint = 'fp-rbe-test-would-be-new-2'"
        )
    assert seen_count == 4
    assert dup_count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_before_emit_scope_narrows_the_match(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_rbe_rows: None,
) -> None:
    """A different scope in the same (source, category) is NOT treated as equivalent.

    Guards against the category-only grain that would over-suppress distinct ideas
    (the risk codex's plan review flagged before this was widened to include scope).
    """
    async with sysgraph_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sysgraph.proposal (source, category, fingerprint, what, seen_count, scope) "
            "VALUES ('reflection', 'rbe-scope-cat', 'fp-rbe-test-scope-a', 'seed', 1, 'orchestrator')"
        )

    result = await sysgraph_repo.read_before_emit(
        "reflection",
        "rbe-scope-cat",
        "tools",
        ProposalRecord(
            source="reflection",
            category="rbe-scope-cat",
            fingerprint="fp-rbe-test-scope-b",
            what="a distinct idea, different scope",
            why=None,
            how=None,
            seen_count=1,
            scope="tools",
        ),
    )

    assert result.decision == "generate_new"
    async with sysgraph_pool.acquire() as conn:
        new_row = await conn.fetchrow(
            "SELECT seen_count FROM sysgraph.proposal WHERE fingerprint = 'fp-rbe-test-scope-b'"
        )
    assert new_row is not None
    assert new_row["seen_count"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_before_emit_generate_new_inserts_row(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_rbe_rows: None,
) -> None:
    """Nothing equivalent exists -> a new row is inserted (control: proves the read wasn't a no-op)."""
    result = await sysgraph_repo.read_before_emit(
        "statistical_detector",
        "rbe-fresh-cat",
        None,
        ProposalRecord(
            source="statistical_detector",
            category="rbe-fresh-cat",
            fingerprint="fp-rbe-test-fresh",
            what="brand new idea",
            why=None,
            how=None,
            seen_count=1,
            scope=None,
        ),
    )

    assert result.decision == "generate_new"
    assert result.proposal_id is not None
    async with sysgraph_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT scope FROM sysgraph.proposal WHERE id = $1", result.proposal_id
        )
    assert row is not None
    assert row["scope"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_before_emit_generation_upsert_increments_not_overwrites(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_rbe_rows: None,
) -> None:
    """A cross-source fingerprint collision increments seen_count, never overwrites it.

    `fingerprint` is derived from (category, scope, what) only — not `source` — so a
    ``reflection``-sourced row and a ``statistical_detector``-sourced row can collide on
    the same fingerprint even though ``find_awaiting_proposal`` (keyed on ``source``) never
    matches them to each other. This exercises the ON CONFLICT (fingerprint) branch of the
    generation-time insert directly. Regression guard for the overwrite bug codex's plan
    review flagged: promotion's own ON CONFLICT clause sets
    ``seen_count = EXCLUDED.seen_count`` (correct only at promotion time, when the caller
    supplies the authoritative accumulated count) — the generation-time path must instead
    increment, never regress an existing higher count back down.
    """
    async with sysgraph_pool.acquire() as conn:
        existing_id = await conn.fetchval(
            "INSERT INTO sysgraph.proposal (source, category, fingerprint, what, seen_count) "
            "VALUES ('reflection', 'rbe-collide-cat', 'fp-rbe-test-collide', 'seed', 5) "
            "RETURNING id"
        )

    result = await sysgraph_repo.read_before_emit(
        "statistical_detector",
        "rbe-collide-cat",
        None,
        ProposalRecord(
            source="statistical_detector",
            category="rbe-collide-cat",
            fingerprint="fp-rbe-test-collide",
            what="seed",
            why=None,
            how=None,
            seen_count=1,
            scope=None,
        ),
    )

    # find_awaiting_proposal is keyed on source=statistical_detector, so the existing
    # reflection-sourced row is invisible to it -> this call takes generate_new, which
    # then hits the fingerprint UNIQUE constraint via ON CONFLICT.
    assert result.decision == "generate_new"
    assert result.proposal_id == existing_id
    async with sysgraph_pool.acquire() as conn:
        seen_count = await conn.fetchval(
            "SELECT seen_count FROM sysgraph.proposal WHERE id = $1", existing_id
        )
    assert seen_count == 6
