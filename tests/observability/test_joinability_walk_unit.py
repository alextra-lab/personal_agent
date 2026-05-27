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
                    "buckets": [{"key": TRACE_A}, {"key": TRACE_B}],
                },
                "no_trace_id": {"doc_count": 0},
            },
        }
    )
    return es


def _green_neo4j() -> Any:
    """Stub a neo4j async driver with one Turn matching TRACE_A."""

    async def aiter() -> Any:
        for r in [
            {
                "turn_id": "t-1",
                "otrace": TRACE_A,
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
    assert set(doc.sampled_trace_ids) == {TRACE_A, TRACE_B}
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
# Tests — red when ES has events for session but no trace_id (§I1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_red_when_es_events_missing_trace_id(ctx: Any) -> None:
    es = MagicMock()
    es.search = AsyncMock(
        return_value={
            "hits": {"total": {"value": 5}},
            "aggregations": {
                "by_trace": {"buckets": [{"key": TRACE_A}]},
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
# Tests — yellow when ES knows a trace_id PG does not (cross-substrate drift)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yellow_when_es_has_extra_trace_ids(ctx: Any) -> None:
    ghost_trace = "44444444-4444-4444-4444-444444444444"
    es = MagicMock()
    es.search = AsyncMock(
        return_value={
            "hits": {"total": {"value": 10}},
            "aggregations": {
                "by_trace": {
                    "buckets": [
                        {"key": TRACE_A},
                        {"key": TRACE_B},
                        {"key": ghost_trace},
                    ]
                },
                "no_trace_id": {"doc_count": 0},
            },
        }
    )
    walk = _build_walk(pg_pool=_green_pg(), es=es, ctx=ctx)
    doc = await walk.run(SESSION_ID, source="cli", window_hours=24, random_seed=0)
    assert doc.outcome == "yellow"
    drift = next(o for o in doc.orphans if o.kind == "three_way_mismatch")
    assert ghost_trace in drift.detail["trace_ids_only_in_es"]


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
                "by_trace": {"buckets": [{"key": TRACE_A}, {"key": TRACE_B}]},
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
