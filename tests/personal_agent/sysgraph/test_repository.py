"""Traversal-helper tests for SysgraphRepository (ADR-0105 D2, FRE-714)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.sysgraph import SysgraphRepository


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
