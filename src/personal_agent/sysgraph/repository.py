"""Isolated System-graph repository (ADR-0105 D2/D3).

The only code path permitted to open a connection to the ``sysgraph``
Postgres schema. Uses raw asyncpg (mirroring ``cost_gate/gate.py`` and
``llm_client/cost_tracker.py``) — a narrow-domain repository over a handful
of recursive-CTE traversals, not general app ORM traffic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import structlog

from personal_agent.config import settings
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn

log = structlog.get_logger(__name__)

_SYSGRAPH_ROLE = "sysgraph_role"

# A bare, unquoted-identifier shape only -- VACUUM cannot bind a table name as a query
# parameter, so this guards the one place a name is interpolated into SQL text. Every caller
# in practice sources names from list_table_names() (the pg_tables catalog itself), never
# external input, but this makes that trust boundary an assertion, not an assumption.
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# A maintenance-sized timeout for VACUUM, independent of the pool's command_timeout=10 (which
# is sized for the fast point queries every other method in this class runs).
_VACUUM_TIMEOUT_SECONDS = 300.0

# ADR-0105 D7: outcome weights for the realized-value signal.
_OUTCOME_WEIGHTS: dict[str, float] = {
    "shipped": 1.0,
    "owner-rejected": -1.0,
    "canceled-as-noise": -0.5,
    "deferred": 0.0,
}

_OutcomeResult = Literal["shipped", "owner-rejected", "canceled-as-noise"]


class _OutcomeAlreadyRecorded(Exception):
    """Internal sentinel — rolls back the transaction when a concurrent caller won the race."""


@dataclass(frozen=True)
class GraphNode:
    """One node reached by a sysgraph traversal."""

    node_type: str
    node_id: UUID
    depth: int


@dataclass(frozen=True)
class SignalValue:
    """Realized-value signal for one (source, category) key (ADR-0105 D7)."""

    value: float
    n: int
    suppressed: bool


_ReadBeforeEmitDecision = Literal["decided_skip", "reinforced", "generate_new"]


@dataclass(frozen=True)
class ReadBeforeEmitResult:
    """Outcome of a generation-time read-before-emit check (ADR-0105 D9/D10)."""

    decision: _ReadBeforeEmitDecision
    proposal_id: UUID | None


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
    scope: str | None = None


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

# ADR-0105 D7 / FRE-717 — outcome ingestion + realized-value signal queries.

_TICKETS_AWAITING_OUTCOME_QUERY = """
SELECT DISTINCT t.linear_issue_id
FROM sysgraph.ticket t
JOIN sysgraph.promoted_to pt ON pt.ticket_id = t.id
WHERE NOT EXISTS (SELECT 1 FROM sysgraph.produced pr WHERE pr.ticket_id = t.id);
"""

_TICKET_SOURCE_KIND_QUERY = """
SELECT p.source, p.category
FROM sysgraph.promoted_to pt
JOIN sysgraph.ticket t ON t.id = pt.ticket_id
JOIN sysgraph.proposal p ON p.id = pt.proposal_id
WHERE t.linear_issue_id = $1
ORDER BY p.created_at DESC;
"""

_OUTCOME_TICKET_ID_QUERY = """
SELECT id FROM sysgraph.ticket WHERE linear_issue_id = $1;
"""

_INSERT_OUTCOME = """
INSERT INTO sysgraph.outcome (result) VALUES ($1) RETURNING id;
"""

_INSERT_PRODUCED_ON_CONFLICT = """
INSERT INTO sysgraph.produced (ticket_id, outcome_id)
VALUES ($1, $2)
ON CONFLICT (ticket_id) DO NOTHING
RETURNING id;
"""

_IS_KIND_DECIDED_QUERY = """
SELECT EXISTS (
    SELECT 1
    FROM sysgraph.produced pr
    JOIN sysgraph.promoted_to pt ON pt.ticket_id = pr.ticket_id
    JOIN sysgraph.proposal p ON p.id = pt.proposal_id
    JOIN sysgraph.outcome o ON o.id = pr.outcome_id
    WHERE p.source = $1 AND p.category = $2 AND o.result != 'deferred'
);
"""

_SIGNAL_OUTCOMES_IN_WINDOW_QUERY = """
SELECT o.result
FROM sysgraph.produced pr
JOIN sysgraph.promoted_to pt ON pt.ticket_id = pr.ticket_id
JOIN sysgraph.proposal p ON p.id = pt.proposal_id
JOIN sysgraph.outcome o ON o.id = pr.outcome_id
WHERE p.source = $1 AND p.category = $2 AND o.observed_at >= $3;
"""

_SIGNAL_SUPPRESSED_UNTIL_QUERY = """
SELECT suppressed_until FROM sysgraph.signal WHERE source = $1 AND category = $2;
"""

_UPSERT_SIGNAL_SUPPRESSION = """
INSERT INTO sysgraph.signal (source, category, suppressed_until)
VALUES ($1, $2, $3)
ON CONFLICT (source, category) DO UPDATE
    SET suppressed_until = EXCLUDED.suppressed_until, updated_at = NOW();
"""

# ADR-0105 D9/D10 / FRE-721 — generation-time read-before-emit queries.
# Deliberately NOT the same upsert as promotion's ON CONFLICT clause: that one
# overwrites seen_count with the caller's authoritative count (correct once,
# at promotion time); this one increments, since a repeated generation-time
# detection of the identical fingerprint must accumulate, never clobber a
# previously-recorded higher count.

_ADVISORY_LOCK_QUERY = "SELECT pg_advisory_xact_lock(hashtext($1));"

_FIND_AWAITING_PROPOSAL_QUERY = """
SELECT id, fingerprint, seen_count
FROM sysgraph.proposal
WHERE source = $1 AND category = $2 AND scope IS NOT DISTINCT FROM $3
ORDER BY created_at DESC
LIMIT 1;
"""

_REINFORCE_PROPOSAL_QUERY = """
UPDATE sysgraph.proposal SET seen_count = seen_count + 1, updated_at = NOW()
WHERE id = $1;
"""

_GENERATION_TIME_UPSERT_PROPOSAL = """
INSERT INTO sysgraph.proposal (source, category, fingerprint, what, why, how, seen_count, scope)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (fingerprint) DO UPDATE
    SET seen_count = sysgraph.proposal.seen_count + 1, updated_at = NOW()
RETURNING id;
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

# ADR-0105 D8/FRE-718: daily maintenance (VACUUM/ANALYZE + a durable completion marker).
_LIST_TABLE_NAMES_QUERY = """
SELECT tablename FROM pg_tables WHERE schemaname = 'sysgraph' ORDER BY tablename;
"""

_INSERT_MAINTENANCE_STAT = """
INSERT INTO sysgraph.stat (name, value, metadata)
VALUES ('sysgraph_maintenance_run', $1, $2::jsonb);
"""

# ADR-0115 D3: the write-time dispatch home for an `output_kind=finding` item. A
# generic, unconstrained `sysgraph.stat` row -- distinct from the richer, dedup-aware
# `owner_diagnostic` Proposal + ticket-linkage pipeline (a separate ticket, FRE-729),
# which may later cite this row as evidence via `sysgraph.derives_from`.
_INSERT_FINDING_STAT = """
INSERT INTO sysgraph.stat (name, value, metadata)
VALUES ('dispatch_finding_observed', 1.0, $1::jsonb);
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
        try:
            async with self.pool.acquire() as conn:
                current_user = await conn.fetchval("SELECT current_user")
        except Exception:
            # The pool was already created — any failure past this point (not just a
            # wrong-role result) must still close it, or a caller whose own connect()
            # try/except doesn't reach disconnect() (there is nothing to disconnect from
            # its perspective — self.pool was never successfully assigned to it) leaks the
            # pool's connections (FRE-718 code review).
            await self.pool.close()
            self.pool = None
            raise
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

    async def tickets_awaiting_outcome(self) -> list[str]:
        """Promoted tickets with no recorded outcome yet (ADR-0105 D7 / FRE-717).

        Returns:
            Linear issue identifiers for every ticket that has a ``PROMOTED_TO``
            edge but no ``PRODUCED`` (outcome) edge — the outcome-ingestion job's
            work queue.
        """
        assert self.pool is not None, "call connect() first"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(_TICKETS_AWAITING_OUTCOME_QUERY)
        return [r["linear_issue_id"] for r in rows]

    async def ticket_source_kind(self, linear_issue_id: str) -> tuple[str, str] | None:
        """Resolve a promoted ticket's ``(source, category)`` via its PROMOTED_TO edge.

        If more than one proposal links to the same ticket (the dedup-matched-
        existing-issue promotion path), the most-recently-created proposal wins
        — logged at INFO so the choice is observable, not silently arbitrary.

        Args:
            linear_issue_id: The Linear ticket identifier.

        Returns:
            ``(source, category)``, or ``None`` if the ticket has no linkage.
        """
        assert self.pool is not None, "call connect() first"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(_TICKET_SOURCE_KIND_QUERY, linear_issue_id)
        if not rows:
            return None
        if len(rows) > 1:
            log.info(
                "sysgraph_ticket_multiple_source_proposals",
                linear_issue_id=linear_issue_id,
                count=len(rows),
            )
        return (rows[0]["source"], rows[0]["category"])

    async def record_outcome(self, linear_issue_id: str, result: _OutcomeResult) -> bool:
        """Record a ticket's terminal outcome, atomically and idempotently (ADR-0105 D7).

        A ticket has exactly one terminal outcome, enforced by
        ``sysgraph.produced``'s ``UNIQUE(ticket_id)`` constraint (migration
        0017) — two concurrent callers racing to record an outcome for the same
        ticket resolve to exactly one winner; the loser's outcome insert is
        rolled back within the same transaction so no orphaned outcome row is
        left behind.

        Args:
            linear_issue_id: The Linear ticket identifier.
            result: The classified terminal outcome.

        Returns:
            ``True`` if this call recorded the outcome, ``False`` if the ticket
            has no sysgraph linkage or already has a recorded outcome.
        """
        assert self.pool is not None, "call connect() first"
        async with self.pool.acquire() as conn:
            ticket_id = await conn.fetchval(_OUTCOME_TICKET_ID_QUERY, linear_issue_id)
            if ticket_id is None:
                log.info("sysgraph_outcome_skipped_no_ticket", linear_issue_id=linear_issue_id)
                return False
            try:
                async with conn.transaction():
                    outcome_id = await conn.fetchval(_INSERT_OUTCOME, result)
                    produced_id = await conn.fetchval(
                        _INSERT_PRODUCED_ON_CONFLICT, ticket_id, outcome_id
                    )
                    if produced_id is None:
                        raise _OutcomeAlreadyRecorded
            except _OutcomeAlreadyRecorded:
                log.info("sysgraph_outcome_already_recorded", linear_issue_id=linear_issue_id)
                return False
        log.info("sysgraph_outcome_recorded", linear_issue_id=linear_issue_id, result=result)
        return True

    async def is_kind_decided(self, source: str, category: str) -> bool:
        """Whether ``(source, category)`` has any non-deferred terminal outcome (ADR-0105 D7).

        Derived on read from outcome existence — no persisted stamp, so it can
        never go stale relative to the outcome data. ``deferred`` never counts
        ("right idea, wrong time" — not a decision). This method exists so
        FRE-721 (T7, D9's generation-time read) has a fact to consult; FRE-717
        only makes it queryable.

        Args:
            source: Proposal source discriminator (ADR-0105 D1).
            category: Proposal category.

        Returns:
            ``True`` if a ``shipped``/``owner-rejected``/``canceled-as-noise``
            outcome has ever been recorded for this key.
        """
        assert self.pool is not None, "call connect() first"
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(_IS_KIND_DECIDED_QUERY, source, category))

    async def get_signal(self, source: str, category: str) -> SignalValue:
        """Compute the windowed realized-value signal for ``(source, category)`` (ADR-0105 D7).

        ``v`` and ``n`` are computed on read from outcome rows within the
        trailing ``signal_window_days`` window — never persisted, so an
        outcome ages out of the window without a compensating write. Only the
        suppression cooldown (set by :meth:`compute_and_apply_signal`) is
        persisted state.

        Args:
            source: Proposal source discriminator (ADR-0105 D1).
            category: Proposal category.

        Returns:
            The current ``SignalValue`` — ``value=0.0, n=0`` when no in-window
            outcomes exist yet.
        """
        assert self.pool is not None, "call connect() first"
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.signal_window_days)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(_SIGNAL_OUTCOMES_IN_WINDOW_QUERY, source, category, cutoff)
            suppressed_until = await conn.fetchval(_SIGNAL_SUPPRESSED_UNTIL_QUERY, source, category)
        n = len(rows)
        total_weight = sum(_OUTCOME_WEIGHTS.get(r["result"], 0.0) for r in rows)
        value = total_weight / (n + settings.signal_smoothing_prior)
        suppressed = suppressed_until is not None and suppressed_until > datetime.now(timezone.utc)
        return SignalValue(value=value, n=n, suppressed=suppressed)

    async def compute_and_apply_signal(self, source: str, category: str) -> SignalValue:
        """Recompute the signal and apply suppression-with-cooldown if triggered (ADR-0105 D7).

        If ``value <= signal_suppression_threshold`` over
        ``n >= signal_suppression_min_n`` in-window outcomes, sets
        ``suppressed_until = now() + signal_suppression_cooldown_days``. A
        cooldown, once started, runs its course — this does *not* clear an
        existing suppression early if the condition no longer holds, matching
        the fixed-duration-timer precedent in ``captains_log/suppression.py``.

        Args:
            source: Proposal source discriminator (ADR-0105 D1).
            category: Proposal category.

        Returns:
            The ``SignalValue`` computed before the suppression upsert (the
            caller's "current v"; the suppression state applies to the *next*
            read).
        """
        signal = await self.get_signal(source, category)
        if (
            signal.value <= settings.signal_suppression_threshold
            and signal.n >= settings.signal_suppression_min_n
        ):
            assert self.pool is not None, "call connect() first"
            suppressed_until = datetime.now(timezone.utc) + timedelta(
                days=settings.signal_suppression_cooldown_days
            )
            async with self.pool.acquire() as conn:
                await conn.execute(_UPSERT_SIGNAL_SUPPRESSION, source, category, suppressed_until)
            log.info(
                "sysgraph_signal_suppressed",
                source=source,
                category=category,
                value=signal.value,
                n=signal.n,
                suppressed_until=suppressed_until.isoformat(),
            )
        return signal

    async def read_before_emit(
        self,
        source: str,
        category: str,
        scope: str | None,
        proposal: ProposalRecord,
    ) -> ReadBeforeEmitResult:
        """Generation-time read-before-emit, transactional (ADR-0105 D9/D10, FRE-721/T7).

        A producer calls this immediately before it would otherwise record a new
        proposal. Branches on the fallback match key ``(source, category, scope)``
        — D10's separation probe (FRE-720) found no clean semantic floor on this
        corpus, so this is category+scope grouping, never vector clustering.

        Serialized per key via a transaction-scoped Postgres advisory lock: no
        unique constraint exists at ``(source, category, scope)`` grain (only
        ``fingerprint`` is unique), so without the lock two concurrent producers
        could both observe "no existing awaiting proposal" and each insert one.

        Args:
            source: Proposal source discriminator (ADR-0105 D1).
            category: Proposal category.
            scope: Proposal scope — the cheap fallback facet (D9); ``None``
                matches only other ``None`` scopes, never a wildcard.
            proposal: Fields for a new proposal row. Only used on the
                ``generate_new`` branch.

        Returns:
            ``decided_skip`` when this ``(source, category)`` kind already has
            a terminal, non-``deferred`` outcome (ADR-0105 D7) — no write.
            ``reinforced`` when an awaiting (not-yet-decided) equivalent
            already exists — its ``seen_count`` is incremented in place,
            ``proposal_id`` is the existing row's id. ``generate_new`` when
            nothing equivalent exists — a new row is inserted, ``proposal_id``
            is the new row's id.
        """
        assert self.pool is not None, "call connect() first"
        lock_key = f"{source}:{category}:{scope or ''}"
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(_ADVISORY_LOCK_QUERY, lock_key)
            decided = bool(await conn.fetchval(_IS_KIND_DECIDED_QUERY, source, category))
            if decided:
                return ReadBeforeEmitResult(decision="decided_skip", proposal_id=None)

            existing = await conn.fetchrow(_FIND_AWAITING_PROPOSAL_QUERY, source, category, scope)
            if existing is not None:
                await conn.execute(_REINFORCE_PROPOSAL_QUERY, existing["id"])
                return ReadBeforeEmitResult(decision="reinforced", proposal_id=existing["id"])

            new_id = await conn.fetchval(
                _GENERATION_TIME_UPSERT_PROPOSAL,
                proposal.source,
                proposal.category,
                proposal.fingerprint,
                proposal.what,
                proposal.why,
                proposal.how,
                proposal.seen_count,
                proposal.scope,
            )
            return ReadBeforeEmitResult(decision="generate_new", proposal_id=new_id)

    async def list_table_names(self) -> list[str]:
        """Return every table name in the ``sysgraph`` schema (ADR-0105 D8/FRE-718).

        Queries ``pg_tables`` rather than a hardcoded list, so a future migration adding a
        table is picked up automatically without this module needing an update.

        Returns:
            Table names (no schema prefix), sorted.
        """
        assert self.pool is not None, "call connect() first"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(_LIST_TABLE_NAMES_QUERY)
        return [r["tablename"] for r in rows]

    async def vacuum_analyze_table(self, table_name: str) -> None:
        """Run ``VACUUM (ANALYZE)`` on one ``sysgraph`` table (ADR-0105 D8/AC-7).

        A plain ``VACUUM`` (no ``FULL``) takes only a ``SHARE UPDATE EXCLUSIVE`` lock, which
        does not block normal reads/writes on the table. Must not be called inside an
        explicit asyncpg transaction — ``VACUUM`` cannot run inside a transaction block at
        all, so this acquires a connection and calls ``execute()`` directly rather than
        opening ``conn.transaction()``. Passes an explicit ``timeout`` overriding the pool's
        ``command_timeout=10`` (sized for fast point queries elsewhere in this class, not a
        maintenance statement) — autovacuum has never run on any sysgraph table yet (see the
        module docstring), so the first real ``VACUUM`` on a bloated table could easily
        exceed 10 seconds and would otherwise fail every day, permanently and silently
        (FRE-718 code review).

        Args:
            table_name: The table to vacuum, unqualified (no schema prefix).

        Raises:
            ValueError: ``table_name`` is not a bare SQL identifier — ``VACUUM`` cannot bind
                a table name as a query parameter, so this guards the one place a name is
                interpolated into SQL text (see ``_SAFE_IDENTIFIER_RE``).
        """
        if not _SAFE_IDENTIFIER_RE.match(table_name):
            raise ValueError(
                f"refusing to VACUUM a non-identifier-shaped table name: {table_name!r}"
            )
        assert self.pool is not None, "call connect() first"
        async with self.pool.acquire() as conn:
            await conn.execute(
                f'VACUUM (ANALYZE) sysgraph."{table_name}"', timeout=_VACUUM_TIMEOUT_SECONDS
            )

    async def vacuum_analyze_all(self) -> dict[str, str]:
        """Run :meth:`vacuum_analyze_table` for every ``sysgraph`` table (ADR-0105 D8/AC-7).

        One failing table does not abort the rest — mirrors ``run_outcome_ingestion``'s
        per-item try/except/continue shape.

        Returns:
            Mapping of table name to ``"ok"`` on success, or the error string on failure.
        """
        results: dict[str, str] = {}
        for table in await self.list_table_names():
            try:
                await self.vacuum_analyze_table(table)
                results[table] = "ok"
            except Exception as exc:
                log.warning("sysgraph_vacuum_table_failed", table=table, error=str(exc))
                results[table] = str(exc)
        return results

    async def record_maintenance_run(self, results: dict[str, str]) -> None:
        """Record a durable, SQL-queryable "last succeeded" marker (ADR-0105 D8/AC-7).

        Inserts a row into ``sysgraph.stat`` (an existing table, unused elsewhere in this
        module) — master's live verification is one query
        (``SELECT * FROM sysgraph.stat WHERE name='sysgraph_maintenance_run' ORDER BY
        observed_at DESC LIMIT 1``), not a log grep.

        Args:
            results: The :meth:`vacuum_analyze_all` output — per-table ``"ok"``/error strings.
        """
        assert self.pool is not None, "call connect() first"
        successful = sum(1 for status in results.values() if status == "ok")
        metadata = json.dumps({"results": results, "table_count": len(results)})
        async with self.pool.acquire() as conn:
            await conn.execute(_INSERT_MAINTENANCE_STAT, float(successful), metadata)

    async def record_finding(
        self,
        *,
        entity_name: str,
        entity_type: str,
        description: str | None,
        trace_id: str | None,
        session_id: str | None,
    ) -> None:
        """Record a write-time-dispatched ``finding`` item (ADR-0115 D1/D3).

        The minimal, always-available home for a per-item ``output_kind=finding``
        entity: an append-only ``sysgraph.stat`` observation. Establishes the
        isolation-by-construction invariant (the item never reaches Core and is
        durably queryable in ``sysgraph``, not dropped) without pre-building the
        richer, dedup-aware ``owner_diagnostic`` Proposal + ticket-linkage
        pipeline (a separate ticket).

        Args:
            entity_name: The extracted entity's name.
            entity_type: The extracted entity's type.
            description: The extracted entity's description, if any.
            trace_id: Originating capture's trace_id, for correlation.
            session_id: Originating capture's session_id, for correlation.
        """
        assert self.pool is not None, "call connect() first"
        metadata = json.dumps(
            {
                "entity_name": entity_name,
                "entity_type": entity_type,
                "description": description,
                "trace_id": trace_id,
                "session_id": session_id,
            }
        )
        async with self.pool.acquire() as conn:
            await conn.execute(_INSERT_FINDING_STAT, metadata)
        log.info(
            "sysgraph_finding_dispatched",
            entity_name=entity_name,
            trace_id=trace_id,
        )
