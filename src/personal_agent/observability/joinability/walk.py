"""Cross-substrate joinability walk (ADR-0074 Phase 5).

A :class:`JoinabilityWalk` is constructed with already-open substrate clients
and a :class:`~personal_agent.telemetry.trace.TraceContext`. Each substrate is
walked in turn; the result is a :class:`ResultDoc` summarising what was found
and any identity violations.

Each substrate walk is wrapped in a ``try/except`` such that one substrate
being unreachable does not abort the whole run — it yellow-marks one check
and the rest of the walk continues. This is the property that makes the
probe's *output gap* (no docs in ES for a day) and *output yellow* (probe
ran, one substrate down) distinguishable signals.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from personal_agent.observability.joinability.result import (
    Orphan,
    ResultDoc,
    SubstrateCheck,
    aggregate_outcome,
)
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.events import MODEL_CALL_COMPLETED

# Loggers whose traceless ES events are expected and out of scope for the gate.
# WS transport events carry session_id for correlation but have no LLM trace.
_TRACELESS_EXCLUDED_LOGGERS: frozenset[str] = frozenset(
    {
        # WS transport — connection lifecycle events carry session_id for
        # correlation but have no LLM trace to attach to.
        "personal_agent.transport.agui.ws_endpoint",
        # Legacy SSE transport module (renamed to ws_endpoint in FRE-388).
        # Pre-WS-deployment sessions in the 7-day window still carry this logger.
        "personal_agent.transport.agui.endpoint",
        # WS ticket minting — pre-authentication transport event; fires on every
        # WS connection before any LLM trace context exists.
        "personal_agent.service.ws_ticket",
    }
)

if TYPE_CHECKING:
    import asyncpg  # type: ignore[import-untyped]
    import redis.asyncio as aioredis
    from elasticsearch import AsyncElasticsearch
    from neo4j import AsyncDriver

    from personal_agent.telemetry.trace import TraceContext

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helper — coerce UUID-shaped values to strings consistently.
# ---------------------------------------------------------------------------


def _as_str(value: Any) -> str:
    """Coerce a UUID-or-string value to its canonical string form."""
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def _normalize_trace_id(value: str) -> str:
    """Canonicalize a trace id to 32 lowercase hex chars, no dashes (ADR-0093 D1).

    Postgres UUID columns round-trip to dashed form on read regardless of how
    the value was written; Elasticsearch and Neo4j store the OTel-canonical
    undashed hex form (telemetry/logger.py's ``format(trace_id, "032x")``).
    Comparing a Postgres-sourced trace id against either substrate requires
    collapsing both to the same shape first — ``uuid.UUID()`` still parses
    this form for the Postgres-bound queries downstream (FRE-1186).
    """
    return value.replace("-", "").lower()


# ---------------------------------------------------------------------------
# JoinabilityWalk
# ---------------------------------------------------------------------------


class JoinabilityWalk:
    """Walk one session across every substrate and assert identity invariants.

    The walk takes already-open clients so a single tick of the brainstem
    scheduler can run the walk without opening/closing pools every hour.

    Attributes:
        pg_pool: asyncpg pool for Postgres (sessions / api_costs / metrics
            / captures / reflections / consolidation / budget / artifacts).
        es: AsyncElasticsearch client for agent-logs-* and agent-captains-*.
        neo4j_driver: Neo4j async driver for ``(:Turn)`` / ``(:Entity)``.
        redis: ``redis.asyncio`` client for stream best-effort checks.
        ctx: System trace context for this probe run (its own identity).
        logs_prefix: Index prefix for ``agent-logs-*`` (test/prod aware).
        captures_prefix: Index prefix for ``agent-captains-*`` captures.
    """

    def __init__(
        self,
        *,
        pg_pool: "asyncpg.Pool | None",
        es: "AsyncElasticsearch | None",
        neo4j_driver: "AsyncDriver | None",
        redis: "aioredis.Redis | None",
        ctx: "TraceContext",
        logs_prefix: str,
        captures_prefix: str,
    ) -> None:
        """Store substrate clients and the trace context for this run."""
        self.pg_pool = pg_pool
        self.es = es
        self.neo4j_driver = neo4j_driver
        self.redis = redis
        self.ctx = ctx
        self.logs_prefix = logs_prefix
        self.captures_prefix = captures_prefix

    # -- Entry point --------------------------------------------------------

    async def run(
        self,
        session_id: str,
        *,
        source: Literal["scheduler", "cli", "ci", "manual"],
        window_hours: int,
        random_seed: int,
    ) -> ResultDoc:
        """Walk one session and return the result document.

        Args:
            session_id: Anchor session id (already selected by the caller).
            source: Caller identity (passed through into the result doc).
            window_hours: Sampling window width (informational only).
            random_seed: Seed used by the sampler (logged for reproducibility).

        Returns:
            The completed :class:`ResultDoc`.
        """
        started_at = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        checks: list[SubstrateCheck] = []
        orphans: list[Orphan] = []
        trace_ids: set[str] = set()

        # 1. Anchor session — if missing or unfetchable, skip the rest.
        anchor = await self._walk_sessions(session_id, checks, orphans)
        if anchor is None:
            return self._build(
                started_at=started_at,
                t0=t0,
                source=source,
                window_hours=window_hours,
                random_seed=random_seed,
                sampled_session_id=None,
                trace_ids=trace_ids,
                checks=checks,
                orphans=orphans,
            )

        raw_user_id = anchor.get("user_id")
        anchor_user_id = _as_str(raw_user_id) if raw_user_id is not None else None

        # 2. Postgres walks.
        await self._walk_api_costs(session_id, trace_ids, checks, orphans)
        await self._walk_metrics(trace_ids, checks, orphans)
        await self._walk_captures(trace_ids, checks, orphans)
        await self._walk_reflections(trace_ids, checks, orphans)
        await self._walk_consolidation(trace_ids, checks)
        await self._walk_budget_reservations(session_id, trace_ids, checks, orphans)
        await self._walk_artifacts(session_id, checks)

        # 3. Elasticsearch walks.
        await self._walk_es_agent_logs(session_id, trace_ids, checks, orphans)
        await self._walk_es_user_identity(session_id, anchor_user_id, checks, orphans)
        await self._walk_es_captures(trace_ids, checks, orphans)
        await self._walk_es_reflections(trace_ids, checks)

        # 4. Neo4j walks.
        await self._walk_neo4j_turns(session_id, trace_ids, checks, orphans)
        await self._walk_neo4j_entities(session_id, checks)
        await self._walk_neo4j_claim_user_identity(session_id, anchor_user_id, checks, orphans)

        # 5. Redis (best-effort).
        await self._walk_redis_streams(trace_ids, checks)

        return self._build(
            started_at=started_at,
            t0=t0,
            source=source,
            window_hours=window_hours,
            random_seed=random_seed,
            sampled_session_id=session_id,
            trace_ids=trace_ids,
            checks=checks,
            orphans=orphans,
        )

    # -- Result assembly ----------------------------------------------------

    def _build(
        self,
        *,
        started_at: datetime,
        t0: float,
        source: Literal["scheduler", "cli", "ci", "manual"],
        window_hours: int,
        random_seed: int,
        sampled_session_id: str | None,
        trace_ids: Iterable[str],
        checks: Sequence[SubstrateCheck],
        orphans: Sequence[Orphan],
    ) -> ResultDoc:
        duration_ms = (time.perf_counter() - t0) * 1000.0
        outcome = aggregate_outcome(
            checks,
            orphans,
            sampled_session_id=sampled_session_id,
        )
        return ResultDoc(
            run_id=str(uuid.uuid4()),
            started_at=started_at,
            duration_ms=duration_ms,
            source=source,
            window_hours=window_hours,
            random_seed=random_seed,
            sampled_session_id=sampled_session_id,
            sampled_trace_ids=sorted(trace_ids),
            substrate_checks=list(checks),
            orphans=list(orphans),
            outcome=outcome,
            trace_id=self.ctx.trace_id,
        )

    # -- Postgres walks -----------------------------------------------------

    async def _walk_sessions(
        self,
        session_id: str,
        checks: list[SubstrateCheck],
        orphans: list[Orphan],
    ) -> Any | None:
        substrate = "postgres.sessions"
        if self.pg_pool is None:
            checks.append(_skipped(substrate, "required", reason="no_pg_pool"))
            return None
        t0 = time.perf_counter()
        try:
            async with self.pg_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT session_id, primary_model_at_creation,
                           model_config_path, messages, user_id
                    FROM sessions WHERE session_id = $1
                    """,
                    _to_uuid(session_id),
                )
        except Exception as exc:  # noqa: BLE001 — yellow check, not crash
            checks.append(_errored(substrate, "required", exc, t0))
            return None
        dur = _dur_ms(t0)
        if row is None:
            checks.append(
                SubstrateCheck(
                    substrate=substrate,
                    expected="required",
                    observed_count=0,
                    status="red",
                    duration_ms=dur,
                )
            )
            orphans.append(
                Orphan(
                    substrate=substrate,
                    kind="missing_anchor",
                    detail={"session_id": session_id},
                    severity="red",
                )
            )
            return None
        status: Literal["green", "yellow", "red", "skipped"] = "green"
        if row["primary_model_at_creation"] is None or row["model_config_path"] is None:
            status = "red"
            orphans.append(
                Orphan(
                    substrate=substrate,
                    kind="missing_identity",
                    detail={
                        "session_id": session_id,
                        "field": "primary_model_at_creation/model_config_path",
                    },
                    severity="red",
                )
            )
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="required",
                observed_count=1,
                status=status,
                duration_ms=dur,
            )
        )
        return row

    async def _walk_api_costs(
        self,
        session_id: str,
        trace_ids: set[str],
        checks: list[SubstrateCheck],
        orphans: list[Orphan],
    ) -> None:
        substrate = "postgres.api_costs"
        if self.pg_pool is None:
            checks.append(_skipped(substrate, "conditional", reason="no_pg_pool"))
            return
        t0 = time.perf_counter()
        try:
            async with self.pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, trace_id, session_id
                    FROM api_costs WHERE session_id = $1
                    """,
                    _to_uuid(session_id),
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(_errored(substrate, "conditional", exc, t0))
            return
        dur = _dur_ms(t0)
        status: Literal["green", "yellow", "red", "skipped"] = "green"
        for r in rows:
            if r["trace_id"] is None:
                status = "red"
                orphans.append(
                    Orphan(
                        substrate=substrate,
                        kind="missing_identity",
                        detail={"row_id": r["id"], "field": "trace_id"},
                        severity="red",
                    )
                )
                continue
            if r["session_id"] is None:
                status = "red"
                orphans.append(
                    Orphan(
                        substrate=substrate,
                        kind="missing_identity",
                        detail={"row_id": r["id"], "field": "session_id"},
                        severity="red",
                    )
                )
                continue
            trace_ids.add(_normalize_trace_id(_as_str(r["trace_id"])))
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="conditional",
                observed_count=len(rows),
                status=status,
                duration_ms=dur,
            )
        )

    async def _walk_metrics(
        self,
        trace_ids: set[str],
        checks: list[SubstrateCheck],
        orphans: list[Orphan],
    ) -> None:
        substrate = "postgres.metrics"
        if self.pg_pool is None or not trace_ids:
            checks.append(_skipped(substrate, "absent_ok", reason="no_trace_ids"))
            return
        t0 = time.perf_counter()
        try:
            uuid_list = [_to_uuid(t) for t in trace_ids]
            async with self.pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, trace_id FROM metrics WHERE trace_id = ANY($1::uuid[])",
                    uuid_list,
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(_errored(substrate, "absent_ok", exc, t0))
            return
        dur = _dur_ms(t0)
        status: Literal["green", "yellow", "red", "skipped"] = "green"
        for r in rows:
            if r["trace_id"] is None:
                status = "red"
                orphans.append(
                    Orphan(
                        substrate=substrate,
                        kind="missing_identity",
                        detail={"row_id": r["id"]},
                        severity="red",
                    )
                )
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="absent_ok",
                observed_count=len(rows),
                status=status,
                duration_ms=dur,
            )
        )

    async def _walk_captures(
        self,
        trace_ids: set[str],
        checks: list[SubstrateCheck],
        orphans: list[Orphan],
    ) -> None:
        substrate = "postgres.captains_log_captures"
        if self.pg_pool is None or not trace_ids:
            checks.append(_skipped(substrate, "conditional", reason="no_trace_ids"))
            return
        t0 = time.perf_counter()
        try:
            uuid_list = [_to_uuid(t) for t in trace_ids]
            async with self.pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT trace_id FROM captains_log_captures WHERE trace_id = ANY($1::uuid[])",
                    uuid_list,
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(_errored(substrate, "conditional", exc, t0))
            return
        dur = _dur_ms(t0)
        status: Literal["green", "yellow", "red", "skipped"] = "green"
        for r in rows:
            if r["trace_id"] is None:
                status = "red"
                orphans.append(
                    Orphan(
                        substrate=substrate,
                        kind="missing_identity",
                        detail={"row": "captures"},
                        severity="red",
                    )
                )
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="conditional",
                observed_count=len(rows),
                status=status,
                duration_ms=dur,
            )
        )

    async def _walk_reflections(
        self,
        trace_ids: set[str],
        checks: list[SubstrateCheck],
        orphans: list[Orphan],
    ) -> None:
        substrate = "postgres.captains_log_reflections"
        if self.pg_pool is None or not trace_ids:
            checks.append(_skipped(substrate, "conditional", reason="no_trace_ids"))
            return
        t0 = time.perf_counter()
        try:
            uuid_list = [_to_uuid(t) for t in trace_ids]
            async with self.pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT trace_id FROM captains_log_reflections WHERE trace_id = ANY($1::uuid[])",
                    uuid_list,
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(_errored(substrate, "conditional", exc, t0))
            return
        dur = _dur_ms(t0)
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="conditional",
                observed_count=len(rows),
                status="green",
                duration_ms=dur,
            )
        )
        # Foreign key is enforced by the schema (REFERENCES captures(trace_id))
        # so we don't dereference orphans here; a missing capture would have
        # been caught in the previous check.

    async def _walk_consolidation(
        self,
        trace_ids: set[str],
        checks: list[SubstrateCheck],
    ) -> None:
        substrate = "postgres.consolidation_attempts"
        if self.pg_pool is None or not trace_ids:
            checks.append(_skipped(substrate, "conditional", reason="no_trace_ids"))
            return
        t0 = time.perf_counter()
        try:
            uuid_list = [_to_uuid(t) for t in trace_ids]
            async with self.pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT trace_id FROM consolidation_attempts WHERE trace_id = ANY($1::uuid[])",
                    uuid_list,
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(_errored(substrate, "conditional", exc, t0))
            return
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="conditional",
                observed_count=len(rows),
                status="green",
                duration_ms=_dur_ms(t0),
            )
        )

    async def _walk_budget_reservations(
        self,
        session_id: str,
        trace_ids: set[str],
        checks: list[SubstrateCheck],
        orphans: list[Orphan],
    ) -> None:
        """Check cost-gate reservations join back to the turn (ADR-0074 §8c, FRE-693).

        A row is an orphan when its ``session_id`` is missing or disagrees with the
        sampled anchor session — a trace_id match alone isn't sufficient joinability
        (AC-12). ``task_id`` is intentionally never checked here: ``NULL`` is the
        correct, expected value for every turn-level reservation (mirrors the
        ``route_traces`` convention — set only for a sub-agent segment row).
        """
        substrate = "postgres.budget_reservations"
        if self.pg_pool is None or not trace_ids:
            checks.append(_skipped(substrate, "conditional", reason="no_trace_ids"))
            return
        t0 = time.perf_counter()
        try:
            uuid_list = [_to_uuid(t) for t in trace_ids]
            async with self.pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT reservation_id, trace_id, session_id, task_id
                    FROM budget_reservations
                    WHERE trace_id = ANY($1::uuid[])
                    """,
                    uuid_list,
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(_errored(substrate, "conditional", exc, t0))
            return
        dur = _dur_ms(t0)
        sampled_uuid = _to_uuid(session_id)
        status: Literal["green", "yellow", "red", "skipped"] = "green"
        for r in rows:
            row_session_id = r["session_id"]
            if row_session_id is None or row_session_id != sampled_uuid:
                status = "red"
                orphans.append(
                    Orphan(
                        substrate=substrate,
                        kind="missing_identity",
                        detail={
                            "reservation_id": str(r["reservation_id"]),
                            "trace_id": str(r["trace_id"]),
                        },
                        severity="red",
                    )
                )
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="conditional",
                observed_count=len(rows),
                status=status,
                duration_ms=dur,
            )
        )

    async def _walk_artifacts(
        self,
        session_id: str,
        checks: list[SubstrateCheck],
    ) -> None:
        substrate = "postgres.artifacts"
        if self.pg_pool is None:
            checks.append(_skipped(substrate, "conditional", reason="no_pg_pool"))
            return
        t0 = time.perf_counter()
        try:
            async with self.pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id FROM artifacts WHERE session_id = $1",
                    _to_uuid(session_id),
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(_errored(substrate, "conditional", exc, t0))
            return
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="conditional",
                observed_count=len(rows),
                status="green",
                duration_ms=_dur_ms(t0),
            )
        )

    # -- Elasticsearch walks ------------------------------------------------

    async def _walk_es_agent_logs(
        self,
        session_id: str,
        trace_ids: set[str],
        checks: list[SubstrateCheck],
        orphans: list[Orphan],
    ) -> None:
        substrate = "elasticsearch.agent_logs"
        if self.es is None:
            checks.append(_skipped(substrate, "required", reason="no_es_client"))
            return
        t0 = time.perf_counter()
        index = f"{self.logs_prefix}-*"
        try:
            response = await self.es.search(
                index=index,
                size=0,
                query={"term": {"session_id": session_id}},
                aggs={
                    "by_trace": {"terms": {"field": "trace_id", "size": 200}},
                    "no_trace_id": {
                        "filter": {
                            "bool": {
                                "must_not": [
                                    {"exists": {"field": "trace_id"}},
                                    *[
                                        {"term": {"logger": lg}}
                                        for lg in sorted(_TRACELESS_EXCLUDED_LOGGERS)
                                    ],
                                ]
                            }
                        }
                    },
                    # Traces that actually billed (FRE-1186 remedy 3): a paid
                    # call's model_call_completed carries cost_usd > 0 regardless
                    # of provider (emit_model_call_completed's `extra`); the local/
                    # free-inference client never sets cost_usd at all, so a free
                    # call is excluded by the range filter automatically. Scoped to
                    # this one event (not just "any doc with cost_usd") so an
                    # unrelated cost-adjacent log line (cost_gate, budget) can't
                    # false-positive a trace into "should have an api_costs row".
                    # Field is event_type, not event: es_logger.py's log_event()
                    # builds ``doc = {..., "event_type": event_type, **data}`` —
                    # structlog's own "event" key is reserved and dropped before
                    # the doc is built (es_handler.py _RESERVED_EVENT_KEYS), so it
                    # is never actually indexed under that name on these docs.
                    # This is what lets us tell a genuine correlation failure (a
                    # trace that billed but never landed an api_costs row —
                    # record_api_call can fail non-fatally, litellm_client.py's
                    # cost_record_failed) apart from a benign system span that was
                    # never billed (consolidation, brainstem ticks, HTTP/WS
                    # lifecycle — the case the comment below already documented).
                    # Known gap (FRE-1205): the gateway streaming-chat path's
                    # own model_call_completed (chat_api.py
                    # _emit_gateway_model_call_completed) never sets cost_usd,
                    # so a correlation failure on THAT path stays yellow below
                    # rather than escalating here — everything routed through
                    # llm_client (LiteLLMClient / LocalLLMClient) is covered.
                    "cost_bearing_trace": {
                        "filter": {
                            "bool": {
                                "filter": [
                                    {"term": {"event_type": MODEL_CALL_COMPLETED}},
                                    {"range": {"cost_usd": {"gt": 0}}},
                                ]
                            }
                        },
                        "aggs": {"by_trace": {"terms": {"field": "trace_id", "size": 200}}},
                    },
                },
                ignore_unavailable=True,
                allow_no_indices=True,
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(_errored(substrate, "required", exc, t0))
            return
        dur = _dur_ms(t0)
        hits = int(response.get("hits", {}).get("total", {}).get("value", 0))
        no_trace_hits = int(
            response.get("aggregations", {}).get("no_trace_id", {}).get("doc_count", 0)
        )
        buckets = response.get("aggregations", {}).get("by_trace", {}).get("buckets", [])
        es_trace_ids = {b["key"] for b in buckets}
        cost_bearing_buckets = (
            response.get("aggregations", {})
            .get("cost_bearing_trace", {})
            .get("by_trace", {})
            .get("buckets", [])
        )
        cost_bearing_trace_ids = {b["key"] for b in cost_bearing_buckets}
        status: Literal["green", "yellow", "red", "skipped"] = "green"
        if no_trace_hits > 0:
            status = "red"
            orphans.append(
                Orphan(
                    substrate=substrate,
                    kind="missing_identity",
                    detail={
                        "session_id": session_id,
                        "events_without_trace_id": no_trace_hits,
                    },
                    severity="red",
                )
            )
        # Trace ids ES knows about that PG didn't surface. Most are benign
        # (system spans: HTTP request traces, background task traces that never
        # bill and so never create an api_costs row) — those stay an
        # informational yellow orphan, unescalated, same as before FRE-1186
        # remedy 3. But a trace that DID bill (cost_bearing_trace_ids, above) and
        # still has no api_costs row is a genuine correlation failure — the
        # write either failed (litellm_client.py's non-fatal cost_record_failed)
        # or never happened — and that is exactly the class ADR-0129 AC-2/AC-3
        # need to catch once correlation is a gated criterion, so it reds.
        unknown_in_es = es_trace_ids - trace_ids
        unjoined_cost_traces = unknown_in_es & cost_bearing_trace_ids
        benign_system_spans = unknown_in_es - unjoined_cost_traces
        if unjoined_cost_traces:
            status = "red"
            orphans.append(
                Orphan(
                    substrate=substrate,
                    kind="es_pg_mismatch",
                    detail={
                        "session_id": session_id,
                        "trace_ids_billed_but_missing_api_costs_row": sorted(unjoined_cost_traces)[
                            :20
                        ],
                    },
                    severity="red",
                )
            )
        if benign_system_spans:
            orphans.append(
                Orphan(
                    substrate=substrate,
                    kind="three_way_mismatch",
                    detail={
                        "session_id": session_id,
                        "trace_ids_only_in_es": sorted(benign_system_spans)[:20],
                    },
                    severity="yellow",
                )
            )
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="required",
                observed_count=hits,
                status=status,
                duration_ms=dur,
            )
        )

    async def _walk_es_user_identity(
        self,
        session_id: str,
        anchor_user_id: str | None,
        checks: list[SubstrateCheck],
        orphans: list[Orphan],
    ) -> None:
        """Compare the anchor session's Postgres user_id against ES log docs (ADR-0107 §6).

        Absent before this ADR (verified: no reference to ``user_id`` anywhere in
        this walk). A wrong ``user_id`` on any log doc for this session is red — a
        regression of the claim-resolution/logging-propagation work this check
        exists to catch (AC-5). Log docs that carry no ``user_id`` at all are
        recorded as an informational orphan only: the coverage/volume bar for
        *missing* (as opposed to *wrong*) user_id is ADR-0107 AC-3a's concern
        (a different ticket), not this joinability check's.
        """
        substrate = "elasticsearch.agent_logs_user_id"
        if anchor_user_id is None:
            checks.append(_skipped(substrate, "conditional", reason="no_anchor_user_id"))
            return
        if self.es is None:
            checks.append(_skipped(substrate, "conditional", reason="no_es_client"))
            return
        t0 = time.perf_counter()
        index = f"{self.logs_prefix}-*"
        try:
            response = await self.es.search(
                index=index,
                size=0,
                query={"term": {"session_id": session_id}},
                aggs={"by_user": {"terms": {"field": "user_id", "size": 10}}},
                ignore_unavailable=True,
                allow_no_indices=True,
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(_errored(substrate, "conditional", exc, t0))
            return
        dur = _dur_ms(t0)
        hits = int(response.get("hits", {}).get("total", {}).get("value", 0))
        buckets = response.get("aggregations", {}).get("by_user", {}).get("buckets", [])
        es_user_ids = {b["key"] for b in buckets if b.get("key")}
        status: Literal["green", "yellow", "red", "skipped"] = "green"
        mismatched = es_user_ids - {anchor_user_id}
        if mismatched:
            status = "red"
            orphans.append(
                Orphan(
                    substrate=substrate,
                    kind="es_pg_mismatch",
                    detail={
                        "session_id": session_id,
                        "postgres_user_id": anchor_user_id,
                        "mismatched_es_user_ids": sorted(mismatched),
                    },
                    severity="red",
                )
            )
        elif hits > 0 and not es_user_ids:
            orphans.append(
                Orphan(
                    substrate=substrate,
                    kind="missing_identity",
                    detail={"session_id": session_id, "docs_without_user_id": hits},
                    severity="yellow",
                )
            )
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="conditional",
                observed_count=len(es_user_ids),
                status=status,
                duration_ms=dur,
            )
        )

    async def _walk_es_captures(
        self,
        trace_ids: set[str],
        checks: list[SubstrateCheck],
        orphans: list[Orphan],
    ) -> None:
        substrate = "elasticsearch.captains_captures"
        if self.es is None or not trace_ids:
            checks.append(_skipped(substrate, "conditional", reason="no_trace_ids"))
            return
        t0 = time.perf_counter()
        index = f"{self.captures_prefix}-captures-*"
        try:
            response = await self.es.search(
                index=index,
                size=0,
                query={"terms": {"trace_id": sorted(trace_ids)}},
                ignore_unavailable=True,
                allow_no_indices=True,
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(_errored(substrate, "conditional", exc, t0))
            return
        hits = int(response.get("hits", {}).get("total", {}).get("value", 0))
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="conditional",
                observed_count=hits,
                status="green",
                duration_ms=_dur_ms(t0),
            )
        )
        # ES↔PG reconciliation deferred to a dedicated check once the
        # Captain's Log canonicalization FRE lands and the doc_id contract
        # is firmer. Mark orphans variable to silence linters.
        _ = orphans

    async def _walk_es_reflections(
        self,
        trace_ids: set[str],
        checks: list[SubstrateCheck],
    ) -> None:
        substrate = "elasticsearch.captains_reflections"
        if self.es is None or not trace_ids:
            checks.append(_skipped(substrate, "conditional", reason="no_trace_ids"))
            return
        t0 = time.perf_counter()
        index = f"{self.captures_prefix}-reflections-*"
        try:
            response = await self.es.search(
                index=index,
                size=0,
                query={"terms": {"trace_id": sorted(trace_ids)}},
                ignore_unavailable=True,
                allow_no_indices=True,
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(_errored(substrate, "conditional", exc, t0))
            return
        hits = int(response.get("hits", {}).get("total", {}).get("value", 0))
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="conditional",
                observed_count=hits,
                status="green",
                duration_ms=_dur_ms(t0),
            )
        )

    # -- Neo4j walks --------------------------------------------------------

    async def _walk_neo4j_turns(
        self,
        session_id: str,
        trace_ids: set[str],
        checks: list[SubstrateCheck],
        orphans: list[Orphan],
    ) -> None:
        substrate = "neo4j.turn"
        if self.neo4j_driver is None:
            checks.append(_skipped(substrate, "conditional", reason="no_neo4j_driver"))
            return
        t0 = time.perf_counter()
        try:
            async with self.neo4j_driver.session() as nsession:
                result = await nsession.run(
                    """
                    MATCH (t:Turn) WHERE t.originating_session_id = $sid
                    RETURN t.turn_id AS turn_id,
                           t.originating_trace_id AS otrace,
                           t.originating_session_id AS osid
                    """,
                    sid=session_id,
                )
                rows = [record.data() async for record in result]
        except Exception as exc:  # noqa: BLE001
            checks.append(_errored(substrate, "conditional", exc, t0))
            return
        dur = _dur_ms(t0)
        status: Literal["green", "yellow", "red", "skipped"] = "green"
        for r in rows:
            otrace = r.get("otrace")
            osid = r.get("osid")
            if otrace is None or osid is None:
                status = "red"
                orphans.append(
                    Orphan(
                        substrate=substrate,
                        kind="missing_identity",
                        detail={"turn_id": r.get("turn_id")},
                        severity="red",
                    )
                )
                continue
            if otrace not in trace_ids:
                # A Neo4j turn that names a trace_id PG never recorded in
                # api_costs. System-spawned turns (consolidation, brainstem,
                # session_summary) legitimately have no api_costs row. Recorded
                # as informational orphans but do NOT escalate check status.
                orphans.append(
                    Orphan(
                        substrate=substrate,
                        kind="three_way_mismatch",
                        detail={"turn_id": r.get("turn_id"), "trace_id": otrace},
                        severity="yellow",
                    )
                )
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="conditional",
                observed_count=len(rows),
                status=status,
                duration_ms=dur,
            )
        )

    async def _walk_neo4j_entities(
        self,
        session_id: str,
        checks: list[SubstrateCheck],
    ) -> None:
        substrate = "neo4j.entity"
        if self.neo4j_driver is None:
            checks.append(_skipped(substrate, "absent_ok", reason="no_neo4j_driver"))
            return
        t0 = time.perf_counter()
        try:
            async with self.neo4j_driver.session() as nsession:
                result = await nsession.run(
                    """
                    MATCH (e:Entity)
                    WHERE e.originating_session_id = $sid
                    RETURN count(e) AS c
                    """,
                    sid=session_id,
                )
                record = await result.single()
                count = int(record["c"]) if record is not None else 0
        except Exception as exc:  # noqa: BLE001
            checks.append(_errored(substrate, "absent_ok", exc, t0))
            return
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="absent_ok",
                observed_count=count,
                status="green",
                duration_ms=_dur_ms(t0),
            )
        )

    async def _walk_neo4j_claim_user_identity(
        self,
        session_id: str,
        anchor_user_id: str | None,
        checks: list[SubstrateCheck],
        orphans: list[Orphan],
    ) -> None:
        """Compare Postgres user_id against the Person a session's Claim attaches to (ADR-0107 §6).

        Conditional on a Claim existing for this session at all — its absence is
        expected and not an orphan (assert_claim, per ADR-0107 §2, only fires on a
        Personal claim being extracted; most sessions produce none).
        """
        substrate = "neo4j.claim_person_user_id"
        if anchor_user_id is None:
            checks.append(_skipped(substrate, "conditional", reason="no_anchor_user_id"))
            return
        if self.neo4j_driver is None:
            checks.append(_skipped(substrate, "conditional", reason="no_neo4j_driver"))
            return
        t0 = time.perf_counter()
        try:
            async with self.neo4j_driver.session() as nsession:
                result = await nsession.run(
                    """
                    MATCH (p:Person)-[:HAS_FACT]->(c:Claim {session_id: $sid})
                    RETURN DISTINCT p.user_id AS user_id
                    """,
                    sid=session_id,
                )
                rows = [record.data() async for record in result]
        except Exception as exc:  # noqa: BLE001
            checks.append(_errored(substrate, "conditional", exc, t0))
            return
        dur = _dur_ms(t0)
        claim_user_ids: set[str] = set()
        status: Literal["green", "yellow", "red", "skipped"] = "green"
        for r in rows:
            row_user_id = r.get("user_id")
            if row_user_id is None:
                # A Claim's Person carrying no user_id at all violates ADR-0052's
                # anchor-by-user_id invariant harder than a mismatch does — never
                # silently drop it from comparison (mirrors _walk_neo4j_turns's
                # treatment of a missing identity field as a red orphan).
                status = "red"
                orphans.append(
                    Orphan(
                        substrate=substrate,
                        kind="missing_identity",
                        detail={"session_id": session_id, "field": "person.user_id"},
                        severity="red",
                    )
                )
                continue
            claim_user_ids.add(_as_str(row_user_id))
        mismatched = claim_user_ids - {anchor_user_id}
        if mismatched:
            status = "red"
            orphans.append(
                Orphan(
                    substrate=substrate,
                    kind="neo4j_pg_mismatch",
                    detail={
                        "session_id": session_id,
                        "postgres_user_id": anchor_user_id,
                        "mismatched_claim_person_user_ids": sorted(mismatched),
                    },
                    severity="red",
                )
            )
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="conditional",
                observed_count=len(rows),
                status=status,
                duration_ms=dur,
            )
        )

    # -- Redis walks --------------------------------------------------------

    async def _walk_redis_streams(
        self,
        trace_ids: set[str],
        checks: list[SubstrateCheck],
    ) -> None:
        substrate = "redis.streams"
        if self.redis is None or not trace_ids:
            checks.append(_skipped(substrate, "absent_ok", reason="no_redis_or_trace_ids"))
            return
        t0 = time.perf_counter()
        try:
            # Streams are MAXLEN-bounded; absence is normal. We probe XLEN
            # to confirm the stream exists and is non-empty as a coarse
            # liveness check, rather than full payload inspection.
            for stream in (
                "stream:request.captured",
                "stream:request.completed",
            ):
                await self.redis.xlen(stream)
        except Exception as exc:  # noqa: BLE001
            checks.append(_errored(substrate, "absent_ok", exc, t0))
            return
        checks.append(
            SubstrateCheck(
                substrate=substrate,
                expected="absent_ok",
                observed_count=0,
                status="green",
                duration_ms=_dur_ms(t0),
            )
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _dur_ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


def _skipped(
    substrate: str,
    expected: Literal["required", "conditional", "absent_ok"],
    *,
    reason: str,
) -> SubstrateCheck:
    return SubstrateCheck(
        substrate=substrate,
        expected=expected,
        observed_count=0,
        status="skipped",
        duration_ms=0.0,
        error=reason,
    )


def _errored(
    substrate: str,
    expected: Literal["required", "conditional", "absent_ok"],
    exc: BaseException,
    t0: float,
) -> SubstrateCheck:
    log.warning(
        "joinability_substrate_error",
        substrate=substrate,
        error=str(exc),
        exc_info=True,
        trace_id="joinability-probe",
    )
    return SubstrateCheck(
        substrate=substrate,
        expected=expected,
        observed_count=0,
        status="yellow",
        duration_ms=_dur_ms(t0),
        error=f"{type(exc).__name__}: {exc}",
    )


def _to_uuid(value: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(value)
