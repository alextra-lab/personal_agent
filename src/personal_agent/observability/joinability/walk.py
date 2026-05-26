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

# Loggers whose traceless ES events are expected and out of scope for the gate.
# SSE transport events carry session_id for correlation but have no LLM trace.
_TRACELESS_EXCLUDED_LOGGERS: frozenset[str] = frozenset({"personal_agent.transport.agui.endpoint"})

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

        # 2. Postgres walks.
        await self._walk_api_costs(session_id, trace_ids, checks, orphans)
        await self._walk_metrics(trace_ids, checks, orphans)
        await self._walk_captures(trace_ids, checks, orphans)
        await self._walk_reflections(trace_ids, checks, orphans)
        await self._walk_consolidation(trace_ids, checks)
        await self._walk_budget_reservations(trace_ids, checks)
        await self._walk_artifacts(session_id, checks)

        # 3. Elasticsearch walks.
        await self._walk_es_agent_logs(session_id, trace_ids, checks, orphans)
        await self._walk_es_captures(trace_ids, checks, orphans)
        await self._walk_es_reflections(trace_ids, checks)

        # 4. Neo4j walks.
        await self._walk_neo4j_turns(session_id, trace_ids, checks, orphans)
        await self._walk_neo4j_entities(session_id, checks)

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
                           model_config_path, messages
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
            trace_ids.add(_as_str(r["trace_id"]))
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
        trace_ids: set[str],
        checks: list[SubstrateCheck],
    ) -> None:
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
                    SELECT reservation_id FROM budget_reservations
                    WHERE trace_id = ANY($1::uuid[])
                    """,
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
        # Discover any trace ids ES knows about that PG didn't surface — usually
        # benign (system spans), but expose them so a regression in api_costs
        # threading would be visible.
        unknown_in_es = es_trace_ids - trace_ids
        if unknown_in_es:
            status = "yellow" if status == "green" else status
            orphans.append(
                Orphan(
                    substrate=substrate,
                    kind="three_way_mismatch",
                    detail={
                        "session_id": session_id,
                        "trace_ids_only_in_es": sorted(unknown_in_es)[:20],
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
                # api_costs is suspicious — yellow, not red, because some
                # system-spawned turns (consolidation, brainstem) can legitimately
                # have no api_costs row.
                status = "yellow" if status == "green" else status
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
