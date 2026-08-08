"""Unit tests for :class:`JoinabilityWalk` with mocked substrate clients.

Each test wires up :class:`AsyncMock` clients with curated responses; the
walk algorithm should aggregate the per-substrate verdicts into one outcome
without needing live infra. See ``tests/integration/test_joinability_walk.py``
for the round-trip against ``make test-infra-up``.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.observability.joinability.walk import JoinabilityWalk
from personal_agent.telemetry.trace import SystemTraceContext

SESSION_ID = "11111111-1111-1111-1111-111111111111"
TRACE_A = "22222222-2222-2222-2222-222222222222"
TRACE_B = "33333333-3333-3333-3333-333333333333"
# ES and Neo4j store trace_id as 32 lowercase hex chars, no dashes (ADR-0093 D1,
# telemetry/logger.py's format(trace_id, "032x")) — Postgres round-trips UUID
# columns to dashed form on read (TRACE_A/TRACE_B above model that). Fixtures
# below must use the *_HEX form wherever they stand in for ES/Neo4j data, or
# they mask the exact cross-substrate mismatch this file's tests exist to catch.
TRACE_A_HEX = uuid.UUID(TRACE_A).hex
TRACE_B_HEX = uuid.UUID(TRACE_B).hex
ANCHOR_USER_ID = "55555555-5555-5555-5555-555555555555"
OTHER_USER_ID = "66666666-6666-6666-6666-666666666666"


# ---------------------------------------------------------------------------
# Fake substrate clients
# ---------------------------------------------------------------------------


class FakePgConn:
    """Async context manager Postgres connection with scripted responses."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        key = _sql_key(sql)
        return self.responses.get(key, None)

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        key = _sql_key(sql)
        return self.responses.get(key, [])


class FakePgPool:
    """asyncpg.Pool stand-in returning a fixed FakePgConn."""

    def __init__(self, conn: FakePgConn) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        @asynccontextmanager
        async def _cm() -> Any:
            yield self._conn

        return _cm()


def _sql_key(sql: str) -> str:
    """Map an SQL query to a stable lookup key by first table mentioned."""
    s = sql.lower()
    for token in (
        "sessions",
        "api_costs",
        "captains_log_captures",
        "captains_log_reflections",
        "consolidation_attempts",
        "budget_reservations",
        "artifacts",
        "metrics",
    ):
        if token in s:
            return token
    return "?"


def _row(**kw: Any) -> Any:
    """Return an object mocking asyncpg's Record (supports r['col'] and r.get)."""
    r = MagicMock()
    r.__getitem__.side_effect = kw.__getitem__
    r.get.side_effect = kw.get
    return r


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx() -> Any:
    return SystemTraceContext.new("joinability_probe_test")


def _build_walk(
    *,
    pg_pool: Any = None,
    es: Any = None,
    neo4j: Any = None,
    redis: Any = None,
    ctx: Any,
) -> JoinabilityWalk:
    return JoinabilityWalk(
        pg_pool=pg_pool,
        es=es,
        neo4j_driver=neo4j,
        redis=redis,
        ctx=ctx,
        logs_prefix="agent-logs-test",
        captures_prefix="agent-captains-test",
    )


def _green_pg(trace_ids: list[str] = [TRACE_A, TRACE_B]) -> FakePgPool:
    """Green-path Postgres fixture: anchor session exists, api_costs healthy."""
    cost_rows = [
        _row(id=i, trace_id=uuid.UUID(t), session_id=uuid.UUID(SESSION_ID))
        for i, t in enumerate(trace_ids, start=1)
    ]
    return FakePgPool(
        FakePgConn(
            {
                "sessions": _row(
                    session_id=uuid.UUID(SESSION_ID),
                    primary_model_at_creation="qwen3-8b-mlx",
                    model_config_path="config/models/qwen3-8b.yaml",
                    messages=[],
                ),
                "api_costs": cost_rows,
                "metrics": [],
                "captains_log_captures": [_row(trace_id=uuid.UUID(t)) for t in trace_ids],
                "captains_log_reflections": [],
                "consolidation_attempts": [],
                "budget_reservations": [],
                "artifacts": [],
            }
        )
    )


def _green_es() -> Any:
    es = MagicMock()
    es.search = AsyncMock(
        return_value={
            "hits": {"total": {"value": 8}},
            "aggregations": {
                "by_trace": {
                    "buckets": [{"key": TRACE_A_HEX}, {"key": TRACE_B_HEX}],
                },
                "no_trace_id": {"doc_count": 0},
            },
        }
    )
    return es


def _green_neo4j() -> Any:
    """Stub a neo4j async driver with one Turn matching TRACE_A (hex form)."""

    async def aiter() -> Any:
        for r in [
            {
                "turn_id": "t-1",
                "otrace": TRACE_A_HEX,
                "osid": SESSION_ID,
            }
        ]:
            yield _MockRecord(r)

    class _RunResult:
        def __aiter__(self) -> Any:
            return aiter()

        async def single(self) -> Any:
            return _MockRecord({"c": 0})

    class _NeoSession:
        async def run(self, *_a: Any, **_kw: Any) -> Any:
            return _RunResult()

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            pass

    class _Driver:
        def session(self) -> Any:
            return _NeoSession()

    return _Driver()


class _MockRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def data(self) -> dict[str, Any]:
        return dict(self._data)


# ---------------------------------------------------------------------------
# Fixtures — user_id consistency check (ADR-0107 §6, AC-5, FRE-740)
# ---------------------------------------------------------------------------


def _green_pg_with_user_id(user_id: str = ANCHOR_USER_ID) -> FakePgPool:
    """Same as _green_pg() but the anchor session row carries a user_id."""
    cost_rows = [_row(id=1, trace_id=uuid.UUID(TRACE_A), session_id=uuid.UUID(SESSION_ID))]
    return FakePgPool(
        FakePgConn(
            {
                "sessions": _row(
                    session_id=uuid.UUID(SESSION_ID),
                    primary_model_at_creation="qwen3-8b-mlx",
                    model_config_path="config/models/qwen3-8b.yaml",
                    messages=[],
                    user_id=uuid.UUID(user_id),
                ),
                "api_costs": cost_rows,
                "metrics": [],
                "captains_log_captures": [],
                "captains_log_reflections": [],
                "consolidation_attempts": [],
                "budget_reservations": [],
                "artifacts": [],
            }
        )
    )


def _es_with_user_id(
    *,
    es_user_ids: list[str] | None = None,
    hits: int = 8,
) -> Any:
    """ES stub whose response depends on which aggregation is requested.

    ``by_user`` (this ticket's new check) gets ``es_user_ids``; any other
    call (agent_logs trace check, captures, reflections) gets the same
    green trace-shaped response the rest of the suite already relies on.
    """

    async def _search(*_a: Any, **kw: Any) -> Any:
        aggs = kw.get("aggs") or {}
        if "by_user" in aggs:
            buckets = [{"key": uid} for uid in (es_user_ids or [])]
            return {
                "hits": {"total": {"value": hits}},
                "aggregations": {"by_user": {"buckets": buckets}},
            }
        return {
            "hits": {"total": {"value": 8}},
            "aggregations": {
                "by_trace": {"buckets": [{"key": TRACE_A_HEX}]},
                "no_trace_id": {"doc_count": 0},
            },
        }

    es = MagicMock()
    es.search = AsyncMock(side_effect=_search)
    return es


def _neo4j_with_claim_user_id(claim_user_ids: list[str] | None) -> Any:
    """Neo4j stub whose response depends on whether the Cypher targets Claim.

    A ``Claim``-matching query (this ticket's new check) yields
    ``claim_user_ids``; any other query (Turn walk, Entity count) gets its
    usual green shape.
    """

    async def _aiter_claims() -> Any:
        for uid in claim_user_ids or []:
            yield _MockRecord({"user_id": uid})

    async def _aiter_turns() -> Any:
        yield _MockRecord({"turn_id": "t-1", "otrace": TRACE_A_HEX, "osid": SESSION_ID})

    class _RunResult:
        def __init__(self, is_claim_query: bool) -> None:
            self._is_claim_query = is_claim_query

        def __aiter__(self) -> Any:
            return _aiter_claims() if self._is_claim_query else _aiter_turns()

        async def single(self) -> Any:
            return _MockRecord({"c": 0})

    class _NeoSession:
        async def run(self, query: str, **_kw: Any) -> Any:
            return _RunResult("Claim" in query)

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            pass

    class _Driver:
        def session(self) -> Any:
            return _NeoSession()

    return _Driver()


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_green_path(ctx: Any) -> None:
    walk = _build_walk(
        pg_pool=_green_pg(),
        es=_green_es(),
        neo4j=_green_neo4j(),
        redis=None,  # absent_ok
        ctx=ctx,
    )
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=42)
    assert doc.outcome == "green", doc.orphans
    assert doc.sampled_session_id == SESSION_ID
    assert set(doc.sampled_trace_ids) == {TRACE_A_HEX, TRACE_B_HEX}
    # Every check should be either green or skipped (absent_ok empties).
    bad = [c for c in doc.substrate_checks if c.status not in ("green", "skipped")]
    assert bad == [], bad


# ---------------------------------------------------------------------------
# Tests — anchor missing → skipped (orphan emitted but no session)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_anchor_reds_with_missing_anchor_orphan(ctx: Any) -> None:
    pool = FakePgPool(FakePgConn({"sessions": None}))
    walk = _build_walk(pg_pool=pool, ctx=ctx)
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    # The walk short-circuits when the anchor row is missing, so the result
    # doc is "skipped" overall (no session walked), but the orphan must be
    # recorded so reviewers can see *why* this run is skipped.
    assert doc.outcome == "skipped"
    assert doc.sampled_session_id is None
    assert any(o.kind == "missing_anchor" for o in doc.orphans)


# ---------------------------------------------------------------------------
# Tests — red path: api_costs with NULL session_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_red_when_api_costs_has_null_session_id(ctx: Any) -> None:
    # One healthy row, one row with NULL session_id (regression on §I4).
    cost_rows = [
        _row(id=1, trace_id=uuid.UUID(TRACE_A), session_id=uuid.UUID(SESSION_ID)),
        _row(id=2, trace_id=uuid.UUID(TRACE_B), session_id=None),
    ]
    pool = FakePgPool(
        FakePgConn(
            {
                "sessions": _row(
                    session_id=uuid.UUID(SESSION_ID),
                    primary_model_at_creation="m",
                    model_config_path="p",
                    messages=[],
                ),
                "api_costs": cost_rows,
                "metrics": [],
                "captains_log_captures": [],
                "captains_log_reflections": [],
                "consolidation_attempts": [],
                "budget_reservations": [],
                "artifacts": [],
            }
        )
    )
    walk = _build_walk(pg_pool=pool, es=_green_es(), ctx=ctx)
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "red"
    orphan = next(
        o
        for o in doc.orphans
        if o.substrate == "postgres.api_costs" and o.kind == "missing_identity"
    )
    assert orphan.severity == "red"


# ---------------------------------------------------------------------------
# Tests — budget_reservations joinability (FRE-693, ADR-0074 §8c, AC-12)
# ---------------------------------------------------------------------------


def _pg_with_budget_reservations(reservation_rows: list[Any]) -> FakePgPool:
    """A green api_costs anchor (so trace_ids is populated) + given reservation rows."""
    return FakePgPool(
        FakePgConn(
            {
                "sessions": _row(
                    session_id=uuid.UUID(SESSION_ID),
                    primary_model_at_creation="m",
                    model_config_path="p",
                    messages=[],
                ),
                "api_costs": [
                    _row(id=1, trace_id=uuid.UUID(TRACE_A), session_id=uuid.UUID(SESSION_ID))
                ],
                "metrics": [],
                "captains_log_captures": [],
                "captains_log_reflections": [],
                "consolidation_attempts": [],
                "budget_reservations": reservation_rows,
                "artifacts": [],
            }
        )
    )


@pytest.mark.asyncio
async def test_budget_reservations_orphan_when_session_id_null(ctx: Any) -> None:
    rows = [
        _row(
            reservation_id="r-1",
            trace_id=uuid.UUID(TRACE_A),
            session_id=None,
            task_id=None,
        )
    ]
    walk = _build_walk(pg_pool=_pg_with_budget_reservations(rows), es=_green_es(), ctx=ctx)
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "red"
    orphan = next(
        o
        for o in doc.orphans
        if o.substrate == "postgres.budget_reservations" and o.kind == "missing_identity"
    )
    assert orphan.severity == "red"


@pytest.mark.asyncio
async def test_budget_reservations_orphan_when_session_id_mismatch(ctx: Any) -> None:
    other_session = "99999999-9999-9999-9999-999999999999"
    rows = [
        _row(
            reservation_id="r-1",
            trace_id=uuid.UUID(TRACE_A),
            session_id=uuid.UUID(other_session),
            task_id=None,
        )
    ]
    walk = _build_walk(pg_pool=_pg_with_budget_reservations(rows), es=_green_es(), ctx=ctx)
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "red"
    orphan = next(
        o
        for o in doc.orphans
        if o.substrate == "postgres.budget_reservations" and o.kind == "missing_identity"
    )
    assert orphan.severity == "red"


@pytest.mark.asyncio
async def test_budget_reservations_no_orphan_when_session_id_matches(ctx: Any) -> None:
    # task_id NULL alone must NOT be an orphan — it is the correct turn-level state.
    rows = [
        _row(
            reservation_id="r-1",
            trace_id=uuid.UUID(TRACE_A),
            session_id=uuid.UUID(SESSION_ID),
            task_id=None,
        )
    ]
    walk = _build_walk(pg_pool=_pg_with_budget_reservations(rows), es=_green_es(), ctx=ctx)
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "green", doc.orphans
    assert not any(o.substrate == "postgres.budget_reservations" for o in doc.orphans)


# ---------------------------------------------------------------------------
# Tests — red when ES has events for session but no trace_id (§I1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_red_when_es_events_missing_trace_id(ctx: Any) -> None:
    es = MagicMock()
    es.search = AsyncMock(
        return_value={
            "hits": {"total": {"value": 5}},
            "aggregations": {
                "by_trace": {"buckets": [{"key": TRACE_A_HEX}]},
                "no_trace_id": {"doc_count": 3},
            },
        }
    )
    walk = _build_walk(pg_pool=_green_pg(), es=es, ctx=ctx)
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "red"
    assert any(
        o.substrate == "elasticsearch.agent_logs"
        and o.kind == "missing_identity"
        and o.severity == "red"
        for o in doc.orphans
    )


# ---------------------------------------------------------------------------
# Tests — three_way_mismatch records orphan but does NOT yellow the outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_green_with_informational_es_extra_trace_ids(ctx: Any) -> None:
    # System spans (HTTP request traces, background-task traces) appear in ES
    # but have no api_costs row. The orphan is recorded for diagnostics but
    # must not prevent the probe from returning green.
    ghost_trace = uuid.uuid4().hex
    es = MagicMock()
    es.search = AsyncMock(
        return_value={
            "hits": {"total": {"value": 10}},
            "aggregations": {
                "by_trace": {
                    "buckets": [
                        {"key": TRACE_A_HEX},
                        {"key": TRACE_B_HEX},
                        {"key": ghost_trace},
                    ]
                },
                "no_trace_id": {"doc_count": 0},
            },
        }
    )
    walk = _build_walk(pg_pool=_green_pg(), es=es, ctx=ctx)
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "green"
    drift = next(o for o in doc.orphans if o.kind == "three_way_mismatch")
    assert drift.detail["trace_ids_only_in_es"] == [ghost_trace], (
        "TRACE_A/TRACE_B are real, api_costs-recorded traces (dashed PG form vs. "
        "hex ES form) — they must NOT show up as ES-only orphans once trace_id "
        "representations are normalized at the comparison boundary."
    )


# ---------------------------------------------------------------------------
# Tests — FRE-1186 cross-substrate trace_id representation normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("22222222-2222-2222-2222-222222222222", "22222222222222222222222222222222"),
        ("22222222222222222222222222222222", "22222222222222222222222222222222"),
        ("22222222-2222-2222-2222-222222222222".upper(), "22222222222222222222222222222222"),
        ("22222222222222222222222222222222".upper(), "22222222222222222222222222222222"),
    ],
    ids=["dashed-lower", "undashed-lower", "dashed-upper", "undashed-upper"],
)
def test_normalize_trace_id_collapses_to_one_canonical_shape(raw: str, expected: str) -> None:
    from personal_agent.observability.joinability.walk import _normalize_trace_id

    assert _normalize_trace_id(raw) == expected


@pytest.mark.asyncio
async def test_green_path_records_no_neo4j_three_way_mismatch(ctx: Any) -> None:
    """A Neo4j turn whose hex otrace matches a dashed-PG trace must not orphan.

    Pre-fix, ``trace_ids`` stayed dashed while ``otrace`` was hex, so
    ``otrace not in trace_ids`` was always true for a real match — every green
    session picked up a spurious ``neo4j.turn`` three_way_mismatch orphan.
    """
    walk = _build_walk(pg_pool=_green_pg(), es=_green_es(), neo4j=_green_neo4j(), ctx=ctx)
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "green"
    assert not any(
        o.substrate == "neo4j.turn" and o.kind == "three_way_mismatch" for o in doc.orphans
    ), doc.orphans


@pytest.mark.asyncio
async def test_es_captures_and_reflections_query_use_normalized_trace_ids(ctx: Any) -> None:
    """The captures/reflections ``terms`` queries must send hex, not dashed, ids.

    ES stores trace_id as undashed hex; a dashed ``terms`` filter never
    matches a real document, silently undercounting ``observed_count`` to 0.
    """
    es = _green_es()
    walk = _build_walk(pg_pool=_green_pg(), es=es, neo4j=_green_neo4j(), ctx=ctx)
    await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)

    for index_fragment in ("agent-captains-test-captures", "agent-captains-test-reflections"):
        call = next(
            c for c in es.search.call_args_list if index_fragment in str(c.kwargs.get("index", ""))
        )
        queried_trace_ids = call.kwargs.get("query", {}).get("terms", {}).get("trace_id", [])
        assert set(queried_trace_ids) == {TRACE_A_HEX, TRACE_B_HEX}, (
            f"{index_fragment} terms query sent {queried_trace_ids!r} — must be the hex "
            "form ES documents are actually indexed under, not the dashed PG form."
        )


# ---------------------------------------------------------------------------
# Tests — yellow when a substrate raises (network blip)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yellow_when_neo4j_raises(ctx: Any) -> None:
    class _DriverFailing:
        def session(self) -> Any:
            raise RuntimeError("neo4j unreachable")

    walk = _build_walk(
        pg_pool=_green_pg(),
        es=_green_es(),
        neo4j=_DriverFailing(),
        ctx=ctx,
    )
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    # No orphans, just one yellow substrate check.
    assert doc.outcome == "yellow"
    yellow = [c for c in doc.substrate_checks if c.status == "yellow"]
    assert any("neo4j.turn" == c.substrate for c in yellow)


# ---------------------------------------------------------------------------
# Tests — reproducibility metadata travels into the result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_propagates(ctx: Any) -> None:
    walk = _build_walk(pg_pool=_green_pg(), es=_green_es(), neo4j=_green_neo4j(), ctx=ctx)
    doc = await walk.run(SESSION_ID, source="scheduler", window_hours=48, random_seed=98765)
    assert doc.source == "scheduler"
    assert doc.window_hours == 48
    assert doc.random_seed == 98765
    assert doc.kind == "system:joinability_probe"
    assert doc.trace_id == ctx.trace_id


# ---------------------------------------------------------------------------
# Tests — transport-layer traceless events excluded from gate (FRE-376 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_es_query_excludes_transport_logger(ctx: Any) -> None:
    """The no_trace_id ES aggregation must exclude agui.ws_endpoint events.

    WebSocket connection lifecycle events (ws.connected, ws.disconnected,
    etc.) log with session_id but no trace_id — they are not LLM calls and
    have no trace to attach to.  Including them in the traceless count causes
    every active user session to red the joinability gate.

    This test pins the fix: the walk's ES query must filter out
    personal_agent.transport.agui.ws_endpoint from the no_trace_id bucket.
    """
    es = MagicMock()
    es.search = AsyncMock(
        return_value={
            "hits": {"total": {"value": 8}},
            "aggregations": {
                "by_trace": {"buckets": [{"key": TRACE_A_HEX}, {"key": TRACE_B_HEX}]},
                "no_trace_id": {"doc_count": 0},
            },
        }
    )
    walk = _build_walk(pg_pool=_green_pg(), es=es, ctx=ctx)
    await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)

    # Inspect the ES search call for agent-logs (not captures/reflections).
    agent_log_call = next(
        c for c in es.search.call_args_list if "agent-logs" in str(c.kwargs.get("index", ""))
    )
    no_trace_filter = str(
        agent_log_call.kwargs.get("aggs", {}).get("no_trace_id", {}).get("filter", {})
    )
    assert "personal_agent.transport.agui.ws_endpoint" in no_trace_filter, (
        "Walk ES query does not exclude agui.ws_endpoint from the no_trace_id count — "
        "WS lifecycle events will falsely red the joinability gate on every session."
    )


# ---------------------------------------------------------------------------
# Tests — user_id consistency check (ADR-0107 §6, AC-5, FRE-740)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_id_check_green_when_es_and_neo4j_match(ctx: Any) -> None:
    walk = _build_walk(
        pg_pool=_green_pg_with_user_id(),
        es=_es_with_user_id(es_user_ids=[ANCHOR_USER_ID]),
        neo4j=_neo4j_with_claim_user_id([ANCHOR_USER_ID]),
        ctx=ctx,
    )
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "green", doc.orphans
    es_check = next(
        c for c in doc.substrate_checks if c.substrate == "elasticsearch.agent_logs_user_id"
    )
    neo4j_check = next(
        c for c in doc.substrate_checks if c.substrate == "neo4j.claim_person_user_id"
    )
    assert es_check.status == "green"
    assert neo4j_check.status == "green"


@pytest.mark.asyncio
async def test_user_id_check_red_on_es_mismatch(ctx: Any) -> None:
    walk = _build_walk(
        pg_pool=_green_pg_with_user_id(),
        es=_es_with_user_id(es_user_ids=[OTHER_USER_ID]),
        neo4j=_neo4j_with_claim_user_id(None),
        ctx=ctx,
    )
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "red"
    orphan = next(
        o
        for o in doc.orphans
        if o.substrate == "elasticsearch.agent_logs_user_id" and o.kind == "es_pg_mismatch"
    )
    assert orphan.severity == "red"
    assert orphan.detail["postgres_user_id"] == ANCHOR_USER_ID
    assert OTHER_USER_ID in orphan.detail["mismatched_es_user_ids"]


@pytest.mark.asyncio
async def test_user_id_check_red_on_neo4j_claim_mismatch(ctx: Any) -> None:
    walk = _build_walk(
        pg_pool=_green_pg_with_user_id(),
        es=_es_with_user_id(es_user_ids=[ANCHOR_USER_ID]),
        neo4j=_neo4j_with_claim_user_id([OTHER_USER_ID]),
        ctx=ctx,
    )
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "red"
    orphan = next(
        o
        for o in doc.orphans
        if o.substrate == "neo4j.claim_person_user_id" and o.kind == "neo4j_pg_mismatch"
    )
    assert orphan.severity == "red"
    assert orphan.detail["postgres_user_id"] == ANCHOR_USER_ID
    assert OTHER_USER_ID in orphan.detail["mismatched_claim_person_user_ids"]


@pytest.mark.asyncio
async def test_user_id_check_red_when_claim_person_has_no_user_id(ctx: Any) -> None:
    # A Claim attached to a Person with no user_id at all violates ADR-0052's
    # anchor-by-user_id invariant harder than a mismatch does — must never be
    # silently dropped from comparison (code review finding, FRE-740).
    walk = _build_walk(
        pg_pool=_green_pg_with_user_id(),
        es=_es_with_user_id(es_user_ids=[ANCHOR_USER_ID]),
        neo4j=_neo4j_with_claim_user_id([None]),
        ctx=ctx,
    )
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "red"
    orphan = next(
        o
        for o in doc.orphans
        if o.substrate == "neo4j.claim_person_user_id" and o.kind == "missing_identity"
    )
    assert orphan.severity == "red"


@pytest.mark.asyncio
async def test_user_id_check_green_when_no_claim_exists(ctx: Any) -> None:
    # No Claim for this session (ADR-0107 §6: "where a Claim exists" is conditional
    # — its absence must not red the probe).
    walk = _build_walk(
        pg_pool=_green_pg_with_user_id(),
        es=_es_with_user_id(es_user_ids=[ANCHOR_USER_ID]),
        neo4j=_neo4j_with_claim_user_id(None),
        ctx=ctx,
    )
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "green", doc.orphans
    neo4j_check = next(
        c for c in doc.substrate_checks if c.substrate == "neo4j.claim_person_user_id"
    )
    assert neo4j_check.status == "green"
    assert neo4j_check.observed_count == 0


@pytest.mark.asyncio
async def test_user_id_check_skipped_when_anchor_has_no_user_id(ctx: Any) -> None:
    # Existing fixture, unmodified — proves no regression for sessions rows that
    # predate this ticket (or a legacy test fixture) carrying no user_id at all.
    walk = _build_walk(pg_pool=_green_pg(), es=_green_es(), neo4j=_green_neo4j(), ctx=ctx)
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "green", doc.orphans
    es_check = next(
        c for c in doc.substrate_checks if c.substrate == "elasticsearch.agent_logs_user_id"
    )
    neo4j_check = next(
        c for c in doc.substrate_checks if c.substrate == "neo4j.claim_person_user_id"
    )
    assert es_check.status == "skipped"
    assert neo4j_check.status == "skipped"


@pytest.mark.asyncio
async def test_user_id_check_informational_orphan_when_es_docs_lack_user_id(ctx: Any) -> None:
    # Session has ES log docs but none carry user_id at all (propagation not yet
    # deployed, or a regression). This must surface for diagnostics but must NOT
    # red/yellow the outcome by itself — that volume/coverage bar belongs to
    # ADR-0107 AC-3a, a different ticket's acceptance criterion, not this probe.
    walk = _build_walk(
        pg_pool=_green_pg_with_user_id(),
        es=_es_with_user_id(es_user_ids=[], hits=5),
        neo4j=_neo4j_with_claim_user_id(None),
        ctx=ctx,
    )
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "green", doc.orphans
    es_check = next(
        c for c in doc.substrate_checks if c.substrate == "elasticsearch.agent_logs_user_id"
    )
    assert es_check.status == "green"
    orphan = next(
        o
        for o in doc.orphans
        if o.substrate == "elasticsearch.agent_logs_user_id" and o.kind == "missing_identity"
    )
    assert orphan.severity == "yellow"


@pytest.mark.asyncio
async def test_user_id_check_yellow_when_es_raises(ctx: Any) -> None:
    es = MagicMock()
    es.search = AsyncMock(side_effect=RuntimeError("es unreachable"))
    walk = _build_walk(
        pg_pool=_green_pg_with_user_id(),
        es=es,
        neo4j=_neo4j_with_claim_user_id([ANCHOR_USER_ID]),
        ctx=ctx,
    )
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "yellow"
    es_check = next(
        c for c in doc.substrate_checks if c.substrate == "elasticsearch.agent_logs_user_id"
    )
    assert es_check.status == "yellow"


@pytest.mark.asyncio
async def test_user_id_check_yellow_when_neo4j_raises(ctx: Any) -> None:
    class _DriverFailing:
        def session(self) -> Any:
            raise RuntimeError("neo4j unreachable")

    walk = _build_walk(
        pg_pool=_green_pg_with_user_id(),
        es=_es_with_user_id(es_user_ids=[ANCHOR_USER_ID]),
        neo4j=_DriverFailing(),
        ctx=ctx,
    )
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "yellow"
    neo4j_check = next(
        c for c in doc.substrate_checks if c.substrate == "neo4j.claim_person_user_id"
    )
    assert neo4j_check.status == "yellow"
