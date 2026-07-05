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
