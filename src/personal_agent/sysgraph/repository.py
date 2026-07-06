"""Isolated System-graph repository (ADR-0105 D2/D3).

The only code path permitted to open a connection to the ``sysgraph``
Postgres schema. Uses raw asyncpg (mirroring ``cost_gate/gate.py`` and
``llm_client/cost_tracker.py``) — a narrow-domain repository over a handful
of recursive-CTE traversals, not general app ORM traffic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import structlog

from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn

log = structlog.get_logger(__name__)

_SYSGRAPH_ROLE = "sysgraph_role"


@dataclass(frozen=True)
class GraphNode:
    """One node reached by a sysgraph traversal."""

    node_type: str
    node_id: UUID
    depth: int


@dataclass(frozen=True)
class ProposalRecord:
    """Fields needed to upsert a ``sysgraph.proposal`` row (ADR-0105 D4)."""

    source: Literal["statistical_detector", "reflection"]
    category: str
    fingerprint: str
    what: str
    why: str | None
    how: str | None
    seen_count: int


_RECORD_PROMOTION_UPSERT_PROPOSAL = """
INSERT INTO sysgraph.proposal (source, category, fingerprint, what, why, how, seen_count)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (fingerprint) DO UPDATE
    SET seen_count = EXCLUDED.seen_count, updated_at = NOW()
RETURNING id;
"""

_RECORD_PROMOTION_UPSERT_TICKET = """
INSERT INTO sysgraph.ticket (linear_issue_id, title)
VALUES ($1, $2)
ON CONFLICT (linear_issue_id) DO NOTHING
RETURNING id;
"""

_RECORD_PROMOTION_SELECT_TICKET = """
SELECT id FROM sysgraph.ticket WHERE linear_issue_id = $1;
"""

_RECORD_PROMOTION_LINK_EDGE = """
INSERT INTO sysgraph.promoted_to (proposal_id, ticket_id)
VALUES ($1, $2)
ON CONFLICT (proposal_id, ticket_id) DO NOTHING;
"""

_TICKET_SOURCE_PROPOSAL_QUERY = """
SELECT p.id
FROM sysgraph.promoted_to pt
JOIN sysgraph.ticket t ON t.id = pt.ticket_id
JOIN sysgraph.proposal p ON p.id = pt.proposal_id
WHERE t.linear_issue_id = $1;
"""


_PROPOSAL_LINEAGE_QUERY = """
WITH RECURSIVE lineage(node_type, node_id, depth) AS (
    SELECT 'proposal'::text, $1::uuid, 0
    UNION ALL
    SELECT next.node_type, next.node_id, lineage.depth + 1
    FROM lineage
    JOIN LATERAL (
        SELECT 'ticket'::text AS node_type, pt.ticket_id AS node_id
        FROM sysgraph.promoted_to pt
        WHERE lineage.node_type = 'proposal' AND pt.proposal_id = lineage.node_id
        UNION ALL
        SELECT 'outcome'::text AS node_type, pr.outcome_id AS node_id
        FROM sysgraph.produced pr
        WHERE lineage.node_type = 'ticket' AND pr.ticket_id = lineage.node_id
    ) AS next ON TRUE
    WHERE lineage.depth < 3
)
SELECT node_type, node_id, depth FROM lineage WHERE depth > 0 ORDER BY depth;
"""

_ONE_HOP_CORRELATIONS_QUERY = """
WITH RECURSIVE neighbors(node_type, node_id, depth) AS (
    SELECT $1::text, $2::uuid, 0
    UNION ALL
    SELECT c.to_node_type, c.to_node_id, neighbors.depth + 1
    FROM neighbors
    JOIN sysgraph.correlates_with c
      ON c.from_node_type = neighbors.node_type AND c.from_node_id = neighbors.node_id
    WHERE neighbors.depth < 1
)
SELECT node_type, node_id, depth FROM neighbors WHERE depth > 0;
"""


class SysgraphRepository:
    """The only code path permitted to open a connection to the sysgraph schema.

    No memory/recall/tutor code path may construct or use this class — enforced
    by ``test_isolation.py``'s import-boundary check (ADR-0105 AC-2c).

    Fail-closed on connect: if the DSN does not resolve to ``sysgraph_role``,
    ``connect()`` raises rather than silently running as a different (possibly
    over-privileged) role. D9's producer-side fail-open behavior (a later
    ticket, T7/FRE-721) governs read *availability* only — it must never be
    read as license to weaken this check.
    """

    def __init__(self, dsn: str) -> None:
        """Initialise the repository.

        Args:
            dsn: Database URL — accepted in either SQLAlchemy or asyncpg form;
                normalised internally. Must resolve to ``sysgraph_role``.
        """
        self.dsn = _normalize_asyncpg_dsn(dsn)
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Open the asyncpg pool and assert the connection is ``sysgraph_role``.

        Raises:
            RuntimeError: if ``SELECT current_user`` != ``sysgraph_role`` — a
                misconfigured DSN must never silently run as a broader-
                privileged role.
        """
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5, command_timeout=10)
        async with self.pool.acquire() as conn:
            current_user = await conn.fetchval("SELECT current_user")
        if current_user != _SYSGRAPH_ROLE:
            await self.pool.close()
            self.pool = None
            raise RuntimeError(
                f"sysgraph connection resolved to role {current_user!r}, expected "
                f"{_SYSGRAPH_ROLE!r}. Refusing to connect — a misconfigured DSN must "
                "never silently weaken the sysgraph isolation boundary (ADR-0105 D2)."
            )
        log.info("sysgraph_connected")

    async def disconnect(self) -> None:
        """Close the asyncpg pool. Call once at app shutdown."""
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
            log.info("sysgraph_disconnected")

    async def proposal_lineage(self, proposal_id: UUID) -> list[GraphNode]:
        """Traverse proposal -> ticket -> outcome (depth capped at 3).

        Recursive CTE per ADR-0105 D2 ("recursive common-table-expressions for
        the shallow proposal-to-ticket-to-outcome ... path").

        Args:
            proposal_id: The source proposal node.

        Returns:
            Every ticket/outcome node reachable from the proposal, ordered by
            depth. Empty if the proposal has not been promoted.
        """
        assert self.pool is not None, "call connect() first"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(_PROPOSAL_LINEAGE_QUERY, proposal_id)
        return [
            GraphNode(node_type=r["node_type"], node_id=r["node_id"], depth=r["depth"])
            for r in rows
        ]

    async def record_promotion(
        self,
        proposal: ProposalRecord,
        linear_issue_id: str,
        ticket_title: str | None = None,
    ) -> None:
        """Upsert proposal + ticket nodes and link them via PROMOTED_TO (ADR-0105 D4/D7).

        One transaction — writes are transactional with promotion (D2). Both the
        proposal (keyed on fingerprint) and the ticket (keyed on linear_issue_id)
        are idempotent upserts, so calling this repeatedly for the same
        proposal/ticket pair never creates duplicate nodes or edges.

        Args:
            proposal: The source proposal's fields.
            linear_issue_id: The Linear ticket identifier just created (or matched
                as an existing duplicate) by the promotion pipeline.
            ticket_title: Optional ticket title for the ticket node.
        """
        assert self.pool is not None, "call connect() first"
        async with self.pool.acquire() as conn, conn.transaction():
            proposal_id = await conn.fetchval(
                _RECORD_PROMOTION_UPSERT_PROPOSAL,
                proposal.source,
                proposal.category,
                proposal.fingerprint,
                proposal.what,
                proposal.why,
                proposal.how,
                proposal.seen_count,
            )
            ticket_id = await conn.fetchval(
                _RECORD_PROMOTION_UPSERT_TICKET, linear_issue_id, ticket_title
            )
            if ticket_id is None:
                ticket_id = await conn.fetchval(_RECORD_PROMOTION_SELECT_TICKET, linear_issue_id)
            await conn.execute(_RECORD_PROMOTION_LINK_EDGE, proposal_id, ticket_id)
        log.info(
            "sysgraph_promotion_linked",
            proposal_id=str(proposal_id),
            ticket_id=str(ticket_id),
            linear_issue_id=linear_issue_id,
        )

    async def ticket_source_proposal(self, linear_issue_id: str) -> UUID | None:
        """Resolve a promoted ticket back to its source proposal id (ADR-0105 D4/AC-3).

        The ticket -> source-proposal-id direction of the bidirectional linkage,
        via the PROMOTED_TO edge.

        Args:
            linear_issue_id: The Linear ticket identifier.

        Returns:
            The source proposal's id, or ``None`` if the ticket has no linkage.
        """
        assert self.pool is not None, "call connect() first"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(_TICKET_SOURCE_PROPOSAL_QUERY, linear_issue_id)
        return row["id"] if row else None

    async def one_hop_correlations(
        self, node_type: Literal["proposal", "stat"], node_id: UUID
    ) -> list[GraphNode]:
        """CORRELATES_WITH neighbors of a node, one hop out.

        Recursive CTE per ADR-0105 D2 ("... and one-hop correlation paths"),
        depth capped at 1.

        Args:
            node_type: Either ``"proposal"`` or ``"stat"``.
            node_id: The source node.

        Returns:
            Every directly-correlated neighbor. Empty if none exist.
        """
        assert self.pool is not None, "call connect() first"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(_ONE_HOP_CORRELATIONS_QUERY, node_type, node_id)
        return [
            GraphNode(node_type=r["node_type"], node_id=r["node_id"], depth=r["depth"])
            for r in rows
        ]
