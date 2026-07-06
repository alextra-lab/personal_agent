"""Traversal-helper tests for SysgraphRepository (ADR-0105 D2, FRE-714)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.sysgraph import SysgraphRepository
from personal_agent.sysgraph.repository import ProposalRecord


@pytest_asyncio.fixture
async def seeded_lineage(sysgraph_pool: asyncpg.Pool) -> AsyncIterator[dict[str, UUID]]:
    """One proposal -> ticket -> outcome chain, cleaned up after the test."""
    async with sysgraph_pool.acquire() as conn:
        proposal_id = await conn.fetchval(
            "INSERT INTO sysgraph.proposal (source, category, fingerprint, what) "
            "VALUES ('reflection', 'test', 'fp-lineage-test', 'test proposal') "
            "RETURNING id"
        )
        ticket_id = await conn.fetchval(
            "INSERT INTO sysgraph.ticket (linear_issue_id, title) "
            "VALUES ('FRE-TEST-LINEAGE', 'test ticket') RETURNING id"
        )
        outcome_id = await conn.fetchval(
            "INSERT INTO sysgraph.outcome (result) VALUES ('shipped') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO sysgraph.promoted_to (proposal_id, ticket_id) VALUES ($1, $2)",
            proposal_id,
            ticket_id,
        )
        await conn.execute(
            "INSERT INTO sysgraph.produced (ticket_id, outcome_id) VALUES ($1, $2)",
            ticket_id,
            outcome_id,
        )
    try:
        yield {"proposal_id": proposal_id, "ticket_id": ticket_id, "outcome_id": outcome_id}
    finally:
        async with sysgraph_pool.acquire() as conn:
            await conn.execute("DELETE FROM sysgraph.proposal WHERE id = $1", proposal_id)
            await conn.execute("DELETE FROM sysgraph.ticket WHERE id = $1", ticket_id)
            await conn.execute("DELETE FROM sysgraph.outcome WHERE id = $1", outcome_id)


@pytest_asyncio.fixture
async def seeded_correlation(sysgraph_pool: asyncpg.Pool) -> AsyncIterator[dict[str, UUID]]:
    """Two proposals linked by one CORRELATES_WITH edge, cleaned up after the test."""
    async with sysgraph_pool.acquire() as conn:
        from_id = await conn.fetchval(
            "INSERT INTO sysgraph.proposal (source, category, fingerprint, what) "
            "VALUES ('reflection', 'test', 'fp-corr-from', 'from proposal') RETURNING id"
        )
        to_id = await conn.fetchval(
            "INSERT INTO sysgraph.proposal (source, category, fingerprint, what) "
            "VALUES ('reflection', 'test', 'fp-corr-to', 'to proposal') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO sysgraph.correlates_with "
            "(from_node_type, from_node_id, to_node_type, to_node_id, weight) "
            "VALUES ('proposal', $1, 'proposal', $2, 0.9)",
            from_id,
            to_id,
        )
    try:
        yield {"from_id": from_id, "to_id": to_id}
    finally:
        async with sysgraph_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sysgraph.proposal WHERE id = ANY($1::uuid[])", [from_id, to_id]
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proposal_lineage_reaches_ticket_and_outcome(
    sysgraph_repo: SysgraphRepository, seeded_lineage: dict[str, UUID]
) -> None:
    """A promoted proposal's lineage includes its ticket (depth 1) and outcome (depth 2)."""
    nodes = await sysgraph_repo.proposal_lineage(seeded_lineage["proposal_id"])

    by_type = {node.node_type: node.node_id for node in nodes}
    assert by_type.get("ticket") == seeded_lineage["ticket_id"]
    assert by_type.get("outcome") == seeded_lineage["outcome_id"]
    depths = {node.node_type: node.depth for node in nodes}
    assert depths["ticket"] == 1
    assert depths["outcome"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proposal_lineage_empty_when_not_promoted(sysgraph_repo: SysgraphRepository) -> None:
    """A proposal with no PROMOTED_TO edge yields an empty lineage, not an error."""
    unpromoted_id = UUID("00000000-0000-0000-0000-000000000001")
    assert await sysgraph_repo.proposal_lineage(unpromoted_id) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_hop_correlations_finds_neighbor(
    sysgraph_repo: SysgraphRepository, seeded_correlation: dict[str, UUID]
) -> None:
    """A CORRELATES_WITH edge is traversed one hop out to its neighbor."""
    neighbors = await sysgraph_repo.one_hop_correlations("proposal", seeded_correlation["from_id"])

    assert len(neighbors) == 1
    assert neighbors[0].node_type == "proposal"
    assert neighbors[0].node_id == seeded_correlation["to_id"]
    assert neighbors[0].depth == 1


@pytest_asyncio.fixture
async def _cleanup_promotion_rows(
    sysgraph_pool: asyncpg.Pool,
) -> AsyncIterator[None]:
    """Delete any proposal/ticket rows this test's fingerprint/linear_issue_id created."""
    try:
        yield
    finally:
        async with sysgraph_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sysgraph.proposal WHERE fingerprint = 'fp-record-promotion-test'"
            )
            await conn.execute(
                "DELETE FROM sysgraph.ticket WHERE linear_issue_id = 'FRE-TEST-RECORD-PROMOTION'"
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_promotion_creates_proposal_ticket_and_edge(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_promotion_rows: None,
) -> None:
    """record_promotion upserts proposal + ticket and links them via PROMOTED_TO (ADR-0105 D4)."""
    proposal = ProposalRecord(
        source="reflection",
        category="reliability",
        fingerprint="fp-record-promotion-test",
        what="Add retry logic",
        why="Improves reliability",
        how="Wrap calls in tenacity",
        seen_count=5,
    )

    await sysgraph_repo.record_promotion(
        proposal, linear_issue_id="FRE-TEST-RECORD-PROMOTION", ticket_title="Add retry logic"
    )

    async with sysgraph_pool.acquire() as conn:
        proposal_row = await conn.fetchrow(
            "SELECT id, seen_count FROM sysgraph.proposal WHERE fingerprint = $1",
            "fp-record-promotion-test",
        )
        ticket_row = await conn.fetchrow(
            "SELECT id FROM sysgraph.ticket WHERE linear_issue_id = $1",
            "FRE-TEST-RECORD-PROMOTION",
        )
        edge_row = await conn.fetchrow(
            "SELECT proposal_id, ticket_id FROM sysgraph.promoted_to "
            "WHERE proposal_id = $1 AND ticket_id = $2",
            proposal_row["id"],
            ticket_row["id"],
        )

    assert proposal_row is not None
    assert proposal_row["seen_count"] == 5
    assert ticket_row is not None
    assert edge_row is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_promotion_is_idempotent(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_promotion_rows: None,
) -> None:
    """Calling record_promotion twice for the same fingerprint/ticket updates, not duplicates."""
    proposal = ProposalRecord(
        source="statistical_detector",
        category="cost",
        fingerprint="fp-record-promotion-test",
        what="Address cost spike",
        why="Cost spike detected",
        how="Investigate and mitigate",
        seen_count=3,
    )

    await sysgraph_repo.record_promotion(
        proposal, linear_issue_id="FRE-TEST-RECORD-PROMOTION", ticket_title="Cost spike"
    )
    updated = ProposalRecord(**{**proposal.__dict__, "seen_count": 4})
    await sysgraph_repo.record_promotion(
        updated, linear_issue_id="FRE-TEST-RECORD-PROMOTION", ticket_title="Cost spike"
    )

    async with sysgraph_pool.acquire() as conn:
        proposal_rows = await conn.fetch(
            "SELECT id, seen_count FROM sysgraph.proposal WHERE fingerprint = $1",
            "fp-record-promotion-test",
        )
        ticket_rows = await conn.fetch(
            "SELECT id FROM sysgraph.ticket WHERE linear_issue_id = $1",
            "FRE-TEST-RECORD-PROMOTION",
        )
        edge_rows = await conn.fetch(
            "SELECT * FROM sysgraph.promoted_to WHERE proposal_id = $1",
            proposal_rows[0]["id"],
        )

    assert len(proposal_rows) == 1
    assert proposal_rows[0]["seen_count"] == 4
    assert len(ticket_rows) == 1
    assert len(edge_rows) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ticket_source_proposal_resolves_reverse_direction(
    sysgraph_repo: SysgraphRepository,
    _cleanup_promotion_rows: None,
) -> None:
    """AC-3: ticket -> source-proposal-id resolves via the PROMOTED_TO edge."""
    proposal = ProposalRecord(
        source="reflection",
        category="reliability",
        fingerprint="fp-record-promotion-test",
        what="Add retry logic",
        why="Improves reliability",
        how="Wrap calls in tenacity",
        seen_count=1,
    )
    await sysgraph_repo.record_promotion(
        proposal, linear_issue_id="FRE-TEST-RECORD-PROMOTION", ticket_title="Add retry logic"
    )

    resolved = await sysgraph_repo.ticket_source_proposal("FRE-TEST-RECORD-PROMOTION")

    assert resolved is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ticket_source_proposal_none_when_not_promoted(
    sysgraph_repo: SysgraphRepository,
) -> None:
    """A ticket id with no PROMOTED_TO edge resolves to None, not an error."""
    assert await sysgraph_repo.ticket_source_proposal("FRE-DOES-NOT-EXIST") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_connect_rejects_wrong_role() -> None:
    """A DSN that doesn't resolve to sysgraph_role must fail closed (not silently connect)."""
    from personal_agent.config import settings
    from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn

    wrong_role_dsn = _normalize_asyncpg_dsn(settings.database_url)  # connects as `agent`
    repo = SysgraphRepository(dsn=wrong_role_dsn)
    with pytest.raises(RuntimeError, match="expected 'sysgraph_role'"):
        await repo.connect()
