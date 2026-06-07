"""Route-trace ledger durable write/read service (FRE-452 / ADR-0088 D6 sink 1).

The ledger is the **direct durable write** of the ADR-0088 observability contract: a
synchronous, bus-independent Postgres write (D8 — durability never depends on the bus).
It writes the seam-neutral :class:`RouteTraceRow` DTO (never an ``ExecutionContext``), so
the future ``observe_topology`` seam reuses this writer unchanged.

Identity is enforced per ADR-0074: a row without ``trace_id``/``session_id`` raises
:class:`MissingIdentityError` before any SQL runs, mirroring ``CostTrackerService``.

**Privacy:** ``user_message_preview`` is the only raw-stimulus field and is populated only
when the ``route_trace_store_preview`` gate is enabled (default off). When stored it lands
in Postgres and therefore in DB backups, replicas, and any pg log that echoes statements —
treat enabling the gate as widening the PII exposure surface accordingly. By default only a
SHA-256 pointer + counts are kept; the full stimulus stays in ``agent-captains-captures-*``
joinable by ``trace_id``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import structlog

from personal_agent.config import settings
from personal_agent.exceptions import MissingIdentityError
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn
from personal_agent.observability.route_trace.types import (
    OrchestrationEvent,
    RouteTraceRow,
)

log = structlog.get_logger(__name__)

# Column order shared by INSERT and SELECT round-trip. ``id``/``created_at`` defaults are
# handled by the table; ``created_at`` is written explicitly for deterministic read-back.
_INSERT_SQL = """
    INSERT INTO route_traces (
        trace_id, session_id, task_id, created_at, schema_version,
        user_message_chars, message_count, user_message_sha256, user_message_preview,
        task_type, complexity, intent_confidence, decomposition_strategy,
        decomposition_reason, degraded_stages, mode, channel, gateway_label,
        model_role, thinking_enabled, routing_history,
        tool_iteration_count, tools_used, skills_loaded,
        sub_agent_count, sub_agents, expansion_strategy,
        delegate_result_passed_to_synthesis,
        orchestration_event, pedagogical_outcomes, final_reply_chars,
        latency_total_ms, latency_breakdown,
        cost_live_usd, cost_authoritative_usd, cost_reconciled,
        input_tokens, output_tokens,
        fallback_triggered, error_type, error_class
    ) VALUES (
        $1, $2, $3, $4, $5,
        $6, $7, $8, $9,
        $10, $11, $12, $13,
        $14, $15, $16, $17, $18,
        $19, $20, $21::jsonb,
        $22, $23, $24,
        $25, $26::jsonb, $27,
        $28,
        $29, $30::jsonb, $31,
        $32, $33::jsonb,
        $34, $35, $36,
        $37, $38,
        $39, $40, $41
    )
    ON CONFLICT (trace_id, task_id) DO NOTHING
"""

_SELECT_SQL = "SELECT * FROM route_traces WHERE trace_id = $1"

_SELECT_BY_SESSION_SQL = (
    "SELECT * FROM route_traces WHERE session_id = $1 ORDER BY created_at DESC LIMIT $2"
)

# Deterministic "label-lie candidate" predicate (FRE-514): the gateway-declared expansion
# plan disagrees with what orchestration actually did — the "lying gateway label" gap
# (FRE-452). It is a *candidate* heuristic, not an authoritative classifier. Kept
# orthogonal to ``fallback_triggered`` (that path carries its own distinct
# ``orchestration_event`` value, so it never overlaps these clauses). Composed only from
# fixed identifiers/literals — no user input is interpolated, so it is injection-safe.
_LABEL_LIE_SQL = (
    "("
    "(decomposition_strategy IS NOT NULL AND decomposition_strategy <> 'single' "
    "AND orchestration_event = 'primary_handled') "
    "OR "
    "(decomposition_strategy = 'single' AND orchestration_event IN "
    "('delegate_called', 'delegate_result_used', 'delegate_result_discarded'))"
    ")"
)


class RouteTraceLedger:
    """Postgres-backed durable store for per-turn route-trace rows."""

    def __init__(self) -> None:
        """Initialise the ledger (no connection until :meth:`connect`)."""
        self.pool: asyncpg.Pool | None = None
        self.db_url = _normalize_asyncpg_dsn(settings.database_url)

    async def connect(self) -> None:
        """Open the asyncpg connection pool (non-fatal on failure, mirrors cost tracker).

        Idempotent: a second call while already connected is a no-op, so a double-connect
        (e.g. the standalone gateway lifespan plus the main-service lifespan) cannot leak
        the first pool.
        """
        if self.pool is not None:
            return
        try:
            self.pool = await asyncpg.create_pool(
                self.db_url, min_size=1, max_size=5, command_timeout=10
            )
            log.info("route_trace_ledger_connected", database="postgresql")
        except Exception as e:
            log.error("route_trace_ledger_connection_failed", error=str(e), exc_info=True)
            self.pool = None

    async def disconnect(self) -> None:
        """Close the connection pool."""
        if self.pool:
            await self.pool.close()
            self.pool = None
            log.info("route_trace_ledger_disconnected")

    async def fetch_authoritative_cost(self, trace_id: UUID) -> tuple[float, int, int]:
        """Return ``SUM(cost_usd, input_tokens, output_tokens)`` from ``api_costs``.

        This is the ADR-0088 D3 authoritative cost for the turn (source of truth), read
        directly from the identity-enforced ``api_costs`` ledger.

        Args:
            trace_id: The turn trace identifier to aggregate.

        Returns:
            ``(cost_usd, input_tokens, output_tokens)``; zeros when no rows or no pool.
        """
        if not self.pool:
            return (0.0, 0, 0)
        row = await self.pool.fetchrow(
            """
            SELECT COALESCE(SUM(cost_usd), 0) AS cost,
                   COALESCE(SUM(input_tokens), 0) AS in_tok,
                   COALESCE(SUM(output_tokens), 0) AS out_tok
            FROM api_costs WHERE trace_id = $1
            """,
            trace_id,
        )
        if row is None:
            return (0.0, 0, 0)
        return (float(row["cost"]), int(row["in_tok"]), int(row["out_tok"]))

    async def write(self, row: RouteTraceRow) -> None:
        """Persist a route-trace row (idempotent on ``(trace_id, task_id)``).

        The conflict key is ``(trace_id, task_id)`` with ``NULLS NOT DISTINCT`` (ADR-0088
        seam): the turn-level write carries ``task_id=None`` and de-duplicates per turn,
        while future per-topology rows de-duplicate per ``(trace_id, task_id)``.

        Args:
            row: The fully-assembled route-trace DTO.

        Raises:
            MissingIdentityError: If ``trace_id`` or ``session_id`` is ``None`` (ADR-0074).
        """
        if row.trace_id is None or row.session_id is None:
            raise MissingIdentityError(
                f"route-trace write requires trace_id and session_id "
                f"(got trace_id={row.trace_id!r}, session_id={row.session_id!r})"
            )
        if not self.pool:
            log.warning("route_trace_ledger_not_connected", trace_id=str(row.trace_id))
            return

        ped = (
            None if row.pedagogical_outcomes is None else json.dumps(list(row.pedagogical_outcomes))
        )
        async with self.pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL,
                row.trace_id,
                row.session_id,
                row.task_id,
                row.created_at,
                row.schema_version,
                row.user_message_chars,
                row.message_count,
                row.user_message_sha256,
                row.user_message_preview,
                row.task_type,
                row.complexity,
                row.intent_confidence,
                row.decomposition_strategy,
                row.decomposition_reason,
                list(row.degraded_stages),
                row.mode,
                row.channel,
                row.gateway_label,
                row.model_role,
                row.thinking_enabled,
                json.dumps(list(row.routing_history)),
                row.tool_iteration_count,
                list(row.tools_used),
                list(row.skills_loaded),
                row.sub_agent_count,
                json.dumps(list(row.sub_agents)),
                row.expansion_strategy,
                row.delegate_result_passed_to_synthesis,
                row.orchestration_event,
                ped,
                row.final_reply_chars,
                row.latency_total_ms,
                json.dumps(row.latency_breakdown) if row.latency_breakdown is not None else None,
                Decimal(str(row.cost_live_usd)),
                Decimal(str(row.cost_authoritative_usd)),
                row.cost_reconciled,
                row.input_tokens,
                row.output_tokens,
                row.fallback_triggered,
                row.error_type,
                row.error_class,
            )
        log.debug(
            "route_trace_written",
            trace_id=str(row.trace_id),
            session_id=str(row.session_id),
            orchestration_event=row.orchestration_event,
            gateway_label=row.gateway_label,
        )

    async def get_by_trace_id(self, trace_id: UUID) -> RouteTraceRow | None:
        """Read a single route-trace row back by ``trace_id``.

        Args:
            trace_id: The turn trace identifier.

        Returns:
            The reconstructed :class:`RouteTraceRow`, or ``None`` if absent/unconnected.
        """
        if not self.pool:
            return None
        record = await self.pool.fetchrow(_SELECT_SQL, trace_id)
        if record is None:
            return None
        return _row_from_record(record)

    async def list_by_session_id(self, session_id: UUID, limit: int = 50) -> list[RouteTraceRow]:
        """Read a session's route-trace rows, newest first (FRE-514).

        Args:
            session_id: The owning session identifier.
            limit: Maximum number of rows to return (caller is responsible for clamping).

        Returns:
            Route-trace rows ordered by ``created_at`` descending; empty when no rows or
            no pool.
        """
        if not self.pool:
            return []
        records = await self.pool.fetch(_SELECT_BY_SESSION_SQL, session_id, limit)
        return [_row_from_record(r) for r in records]

    async def list_recent(
        self,
        *,
        limit: int = 50,
        label_lie: bool = False,
        fallback_triggered: bool = False,
        not_reconciled: bool = False,
    ) -> list[RouteTraceRow]:
        """Read the most recent route-trace rows, with optional boundary filters (FRE-514).

        The three filters make the deterministic-shell boundary queryable and compose with
        ``AND`` when more than one is set:

        - ``fallback_triggered``: ``fallback_triggered = TRUE`` (exact column).
        - ``not_reconciled``: ``cost_reconciled = FALSE`` (exact column).
        - ``label_lie``: the gateway-declared expansion plan disagrees with the actual
          orchestration event (:data:`_LABEL_LIE_SQL` — a *candidate* heuristic).

        Args:
            limit: Maximum number of rows to return (caller is responsible for clamping).
            label_lie: When ``True``, restrict to label-lie candidates.
            fallback_triggered: When ``True``, restrict to turns that escalated to the primary.
            not_reconciled: When ``True``, restrict to turns whose live/authoritative cost
                disagreed.

        Returns:
            Route-trace rows ordered by ``created_at`` descending; empty when no rows or
            no pool.
        """
        if not self.pool:
            return []
        clauses: list[str] = []
        if fallback_triggered:
            clauses.append("fallback_triggered = TRUE")
        if not_reconciled:
            clauses.append("cost_reconciled = FALSE")
        if label_lie:
            clauses.append(_LABEL_LIE_SQL)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        # Only fixed module-level fragments are interpolated; ``limit`` is a bound param.
        sql = f"SELECT * FROM route_traces{where} ORDER BY created_at DESC LIMIT $1"
        records = await self.pool.fetch(sql, limit)
        return [_row_from_record(r) for r in records]


def _loads(value: Any) -> Any:
    """Decode a JSONB column (asyncpg returns it as ``str``) into Python, else passthrough."""
    if isinstance(value, str):
        return json.loads(value)
    return value


def _row_from_record(record: asyncpg.Record) -> RouteTraceRow:
    """Reconstruct a :class:`RouteTraceRow` from a Postgres record."""
    routing_history = _loads(record["routing_history"]) or []
    sub_agents = _loads(record["sub_agents"]) or []
    latency_breakdown = _loads(record["latency_breakdown"])
    ped = _loads(record["pedagogical_outcomes"])
    return RouteTraceRow(
        trace_id=record["trace_id"],
        session_id=record["session_id"],
        task_id=record["task_id"],
        created_at=record["created_at"],
        schema_version=record["schema_version"],
        user_message_chars=record["user_message_chars"],
        message_count=record["message_count"],
        user_message_sha256=record["user_message_sha256"],
        user_message_preview=record["user_message_preview"],
        task_type=record["task_type"],
        complexity=record["complexity"],
        intent_confidence=record["intent_confidence"],
        decomposition_strategy=record["decomposition_strategy"],
        decomposition_reason=record["decomposition_reason"],
        degraded_stages=tuple(record["degraded_stages"] or ()),
        mode=record["mode"],
        channel=record["channel"],
        gateway_label=record["gateway_label"],
        model_role=record["model_role"],
        thinking_enabled=record["thinking_enabled"],
        routing_history=tuple(routing_history),
        tool_iteration_count=record["tool_iteration_count"],
        tools_used=tuple(record["tools_used"] or ()),
        skills_loaded=tuple(record["skills_loaded"] or ()),
        sub_agent_count=record["sub_agent_count"],
        sub_agents=tuple(sub_agents),
        expansion_strategy=record["expansion_strategy"],
        delegate_result_passed_to_synthesis=record["delegate_result_passed_to_synthesis"],
        orchestration_event=cast(OrchestrationEvent, record["orchestration_event"]),
        pedagogical_outcomes=None if ped is None else tuple(ped),
        final_reply_chars=record["final_reply_chars"],
        latency_total_ms=record["latency_total_ms"],
        latency_breakdown=latency_breakdown,
        cost_live_usd=float(record["cost_live_usd"]),
        cost_authoritative_usd=float(record["cost_authoritative_usd"]),
        cost_reconciled=record["cost_reconciled"],
        input_tokens=record["input_tokens"],
        output_tokens=record["output_tokens"],
        fallback_triggered=record["fallback_triggered"],
        error_type=record["error_type"],
        error_class=record["error_class"],
    )


# Module-level singleton, mirroring the cost-tracker / cost-gate accessor pattern.
route_trace_ledger = RouteTraceLedger()


def get_route_trace_ledger() -> RouteTraceLedger:
    """Return the process-wide route-trace ledger singleton."""
    return route_trace_ledger
