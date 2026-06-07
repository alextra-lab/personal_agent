"""Tests for the gateway sub-agent captures REST read surface (FRE-519).

Exercises the three ``/api/v1/observations/sub-agents/*`` endpoints with a patched
``app.state.es_client`` (no real Elasticsearch). Covers happy paths, empty-200 for
non-decomposed turns, the ``failed_only`` filter, limit clamping, and the 503s.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.gateway.app import create_gateway_router


def _hit(**source: Any) -> dict[str, Any]:
    return {
        "_id": f"{source.get('trace_id', 't')}:{source.get('task_id', 'sub-x')}",
        "_source": source,
    }


def _es_returning(hits: list[dict[str, Any]]) -> AsyncMock:
    es = AsyncMock()
    es.search = AsyncMock(return_value={"hits": {"hits": hits}})
    return es


def _app(es: AsyncMock | None) -> FastAPI:
    app = FastAPI()
    app.include_router(create_gateway_router())
    app.state.es_client = es
    return app


def _client(es: AsyncMock | None) -> TestClient:
    return TestClient(_app(es), raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# GET /{trace_id}
# ---------------------------------------------------------------------------


def test_returns_sub_agents_for_trace() -> None:
    tid = str(uuid4())
    es = _es_returning(
        [
            _hit(trace_id=tid, task_id="sub-a", injected_digest="da"),
            _hit(trace_id=tid, task_id="sub-b", injected_digest="db"),
        ]
    )
    with _client(es) as client:
        resp = client.get(f"/api/v1/observations/sub-agents/{tid}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"] == tid
    assert body["count"] == 2
    assert body["sub_agents"][0]["task_id"] == "sub-a"
    assert body["sub_agents"][0]["injected_digest"] == "da"
    # Queried the sub-agents captures index family.
    index_arg = es.search.call_args.kwargs["index"]
    assert index_arg.endswith("-subagents-*")


def test_empty_when_no_subagents() -> None:
    es = _es_returning([])
    with _client(es) as client:
        resp = client.get(f"/api/v1/observations/sub-agents/{uuid4()}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["sub_agents"] == []


# ---------------------------------------------------------------------------
# GET /session/{session_id}
# ---------------------------------------------------------------------------


def test_session_endpoint_returns_rows() -> None:
    sid = str(uuid4())
    es = _es_returning([_hit(trace_id="t1", task_id="sub-a", session_id=sid)])
    with _client(es) as client:
        resp = client.get(f"/api/v1/observations/sub-agents/session/{sid}")

    assert resp.status_code == 200
    assert len(resp.json()) == 1
    body_query = es.search.call_args.kwargs["body"]
    assert body_query["query"] == {"term": {"session_id": sid}}


# ---------------------------------------------------------------------------
# GET /recent
# ---------------------------------------------------------------------------


def test_recent_endpoint() -> None:
    es = _es_returning([_hit(trace_id="t1", task_id="sub-a")])
    with _client(es) as client:
        resp = client.get("/api/v1/observations/sub-agents/recent")

    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_recent_failed_only_adds_success_filter() -> None:
    es = _es_returning([])
    with _client(es) as client:
        resp = client.get("/api/v1/observations/sub-agents/recent?failed_only=true")

    assert resp.status_code == 200
    body_query = es.search.call_args.kwargs["body"]
    assert {"term": {"success": False}} in body_query["query"]["bool"]["must"]


def test_recent_limit_clamped() -> None:
    es = _es_returning([])
    with _client(es) as client:
        resp = client.get("/api/v1/observations/sub-agents/recent?limit=9999")

    assert resp.status_code == 200
    assert es.search.call_args.kwargs["body"]["size"] <= 200


def test_recent_limit_zero_is_400() -> None:
    es = _es_returning([])
    with _client(es) as client:
        resp = client.get("/api/v1/observations/sub-agents/recent?limit=0")

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 503s
# ---------------------------------------------------------------------------


def test_503_when_es_unavailable() -> None:
    with _client(None) as client:
        resp = client.get(f"/api/v1/observations/sub-agents/{uuid4()}")
    assert resp.status_code == 503


def test_503_on_es_error() -> None:
    es = AsyncMock()
    es.search = AsyncMock(side_effect=RuntimeError("ES down"))
    with _client(es) as client:
        resp = client.get(f"/api/v1/observations/sub-agents/{uuid4()}")
    assert resp.status_code == 503
