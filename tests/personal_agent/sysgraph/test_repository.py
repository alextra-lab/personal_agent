"""Traversal-helper tests for SysgraphRepository (ADR-0105 D2, FRE-714)."""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# Maintenance (ADR-0105 D8/AC-7, FRE-718)
# ---------------------------------------------------------------------------

_EXPECTED_SYSGRAPH_TABLES = {
    "correlates_with",
    "derives_from",
    "influence",
    "outcome",
    "produced",
    "promoted_to",
    "proposal",
    "signal",
    "stat",
    "ticket",
}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_table_names_returns_all_known_sysgraph_tables(
    sysgraph_repo: SysgraphRepository,
) -> None:
    """list_table_names() discovers every table via pg_tables, not a hardcoded list."""
    assert set(await sysgraph_repo.list_table_names()) == _EXPECTED_SYSGRAPH_TABLES


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vacuum_analyze_table_succeeds_on_a_real_table(
    sysgraph_repo: SysgraphRepository,
) -> None:
    """VACUUM (ANALYZE) genuinely executes against a real Postgres table."""
    await sysgraph_repo.vacuum_analyze_table("proposal")  # no exception raised


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vacuum_analyze_table_rejects_a_non_identifier_name(
    sysgraph_repo: SysgraphRepository,
) -> None:
    """A non-identifier-shaped table name is refused before it reaches SQL text (injection guard)."""
    with pytest.raises(ValueError, match="non-identifier-shaped"):
        await sysgraph_repo.vacuum_analyze_table("proposal; DROP TABLE sysgraph.proposal")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vacuum_analyze_all_reports_ok_for_every_table(
    sysgraph_repo: SysgraphRepository,
) -> None:
    """vacuum_analyze_all() returns {table: "ok"} for every sysgraph table."""
    results = await sysgraph_repo.vacuum_analyze_all()
    assert set(results) == _EXPECTED_SYSGRAPH_TABLES
    assert all(status == "ok" for status in results.values())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vacuum_analyze_all_continues_after_one_table_fails(
    sysgraph_repo: SysgraphRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failing table is reported as an error string without aborting the rest."""
    tables = await sysgraph_repo.list_table_names()
    bad_table = tables[0]
    original = sysgraph_repo.vacuum_analyze_table

    async def flaky_vacuum(table_name: str) -> None:
        if table_name == bad_table:
            raise RuntimeError("simulated vacuum failure")
        await original(table_name)

    monkeypatch.setattr(sysgraph_repo, "vacuum_analyze_table", flaky_vacuum)

    results = await sysgraph_repo.vacuum_analyze_all()

    assert results[bad_table] == "simulated vacuum failure"
    assert all(status == "ok" for table, status in results.items() if table != bad_table)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_maintenance_run_inserts_a_queryable_stat_row(
    sysgraph_repo: SysgraphRepository, sysgraph_pool: asyncpg.Pool
) -> None:
    """AC-7's "last succeeded" evidence: a durable, SQL-queryable sysgraph.stat row."""
    try:
        await sysgraph_repo.record_maintenance_run({"proposal": "ok", "ticket": "ok"})

        async with sysgraph_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value, metadata FROM sysgraph.stat "
                "WHERE name = 'sysgraph_maintenance_run' ORDER BY observed_at DESC LIMIT 1"
            )
        assert row is not None
        assert row["value"] == 2.0
        metadata = json.loads(row["metadata"])
        assert metadata["table_count"] == 2
        assert metadata["results"] == {"proposal": "ok", "ticket": "ok"}
    finally:
        async with sysgraph_pool.acquire() as conn:
            await conn.execute("DELETE FROM sysgraph.stat WHERE name = 'sysgraph_maintenance_run'")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_connect_closes_the_pool_on_any_role_check_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (FRE-718 code review): a role-check failure must still close the pool.

    Not just a wrong-role result -- a failure during the role-check itself must still close
    the pool it just created, or a caller whose own connect() try/except has nothing to
    disconnect (self.pool was never assigned on its side) leaks the pool's connections.
    """
    from personal_agent.config import settings

    repo = SysgraphRepository(dsn=settings.sysgraph_database_url)

    class _FailingConn:
        async def fetchval(self, *_args: object, **_kwargs: object) -> str:
            raise RuntimeError("simulated role-check failure")

    class _FailingAcquireCtx:
        async def __aenter__(self) -> _FailingConn:
            return _FailingConn()

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    class _FailingPool:
        def __init__(self) -> None:
            self.closed = False

        def acquire(self) -> _FailingAcquireCtx:
            return _FailingAcquireCtx()

        async def close(self) -> None:
            self.closed = True

    fake_pool = _FailingPool()

    async def fake_create_pool(*_args: object, **_kwargs: object) -> _FailingPool:
        return fake_pool

    monkeypatch.setattr("personal_agent.sysgraph.repository.asyncpg.create_pool", fake_create_pool)

    with pytest.raises(RuntimeError, match="simulated role-check failure"):
        await repo.connect()

    assert repo.pool is None
    assert fake_pool.closed is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vacuum_analyze_table_uses_a_maintenance_sized_timeout(
    sysgraph_repo: SysgraphRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (FRE-718 code review): VACUUM must not inherit the point-query timeout.

    The pool's command_timeout=10 is sized for fast point queries -- autovacuum has never run
    on any sysgraph table, so the first real VACUUM on a bloated table could easily exceed 10
    seconds and would otherwise fail every day, permanently and silently.
    """
    assert sysgraph_repo.pool is not None
    captured: dict[str, object] = {}
    real_pool = sysgraph_repo.pool

    class _CapturingConn:
        def __init__(self, real_conn: object) -> None:
            self._real_conn = real_conn

        async def execute(self, query: str, *args: object, **kwargs: object) -> object:
            captured["query"] = query
            captured["timeout"] = kwargs.get("timeout")
            return await self._real_conn.execute(query, *args, **kwargs)  # type: ignore[attr-defined]

    class _CapturingAcquireCtx:
        def __init__(self, real_ctx: object) -> None:
            self._real_ctx = real_ctx

        async def __aenter__(self) -> _CapturingConn:
            real_conn = await self._real_ctx.__aenter__()  # type: ignore[attr-defined]
            return _CapturingConn(real_conn)

        async def __aexit__(self, *exc: object) -> object:
            return await self._real_ctx.__aexit__(*exc)  # type: ignore[attr-defined]

    # asyncpg.Pool is a C-extension-backed class -- its `acquire` attribute can't be
    # monkeypatched directly (read-only). Wrap the pool instance instead; `pool` is a plain
    # attribute on the repository, so replacing it is safe and auto-reverted by monkeypatch.
    class _CapturingPool:
        def acquire(self) -> _CapturingAcquireCtx:
            return _CapturingAcquireCtx(real_pool.acquire())

    monkeypatch.setattr(sysgraph_repo, "pool", _CapturingPool())

    await sysgraph_repo.vacuum_analyze_table("proposal")

    assert captured["timeout"] is not None
    assert float(captured["timeout"]) > 10.0  # strictly greater than the pool's default
