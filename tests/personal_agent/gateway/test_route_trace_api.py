"""Tests for the gateway route-trace REST read surface (FRE-514).

Exercises the three ``/api/v1/observations/route-traces/*`` endpoints with a patched
:class:`RouteTraceLedger` singleton (no real Postgres). Covers happy paths, the three
``recent`` filters, server-side limit clamping, bad-UUID status codes, and the 503 when
the ledger pool is unconnected.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.gateway.app import create_gateway_router
from personal_agent.observability.route_trace.types import RouteTraceRow

_LEDGER_PATH = "personal_agent.gateway.route_trace_api.get_route_trace_ledger"


def _row(**overrides: object) -> RouteTraceRow:
    base: dict[str, object] = dict(
        trace_id=uuid4(),
        session_id=uuid4(),
        created_at=datetime.now(timezone.utc),
        orchestration_event="primary_handled",
        gateway_label="memory_recall/single",
        task_type="memory_recall",
        decomposition_strategy="single",
    )
    base.update(overrides)
    return RouteTraceRow(**base)  # type: ignore[arg-type]


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(create_gateway_router())
    return app


def _mock_ledger() -> AsyncMock:
    """A connected (truthy ``pool``) ledger mock with async read methods."""
    ledger = AsyncMock()
    ledger.pool = object()  # truthy → not treated as unconnected
    return ledger


# ---------------------------------------------------------------------------
# GET /{trace_id}
# ---------------------------------------------------------------------------


def test_get_by_trace_id_returns_all_rows() -> None:
    """FRE-517: the endpoint returns the trace's rows (turn-level + segments) as a list."""
    tid = uuid4()
    turn_level = _row(trace_id=tid)
    segment = _row(trace_id=tid, task_id=uuid4(), model_role="sub_agent")
    ledger = _mock_ledger()
    ledger.get_by_trace_id.return_value = [turn_level, segment]

    with patch(_LEDGER_PATH, return_value=ledger):
        with TestClient(_app(), raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/observations/route-traces/{tid}")

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and len(body) == 2
    assert body[0]["trace_id"] == str(tid)
    assert body[0]["gateway_label"] == "memory_recall/single"
    assert body[1]["model_role"] == "sub_agent"
    ledger.get_by_trace_id.assert_awaited_once()


def test_get_by_trace_id_404_when_absent() -> None:
    ledger = _mock_ledger()
    ledger.get_by_trace_id.return_value = []

    with patch(_LEDGER_PATH, return_value=ledger):
        with TestClient(_app(), raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/observations/route-traces/{uuid4()}")

    assert resp.status_code == 404


def test_get_by_trace_id_404_on_bad_uuid() -> None:
    ledger = _mock_ledger()

    with patch(_LEDGER_PATH, return_value=ledger):
        with TestClient(_app(), raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/observations/route-traces/not-a-uuid")

    assert resp.status_code == 404
    ledger.get_by_trace_id.assert_not_awaited()


# ---------------------------------------------------------------------------
# GET /session/{session_id}
# ---------------------------------------------------------------------------


def test_list_by_session_returns_rows() -> None:
    sid = uuid4()
    ledger = _mock_ledger()
    ledger.list_by_session_id.return_value = [_row(session_id=sid), _row(session_id=sid)]

    with patch(_LEDGER_PATH, return_value=ledger):
        with TestClient(_app(), raise_server_exceptions=True) as client:
            resp = client.get(f"/api/v1/observations/route-traces/session/{sid}?limit=5")

    assert resp.status_code == 200
    assert len(resp.json()) == 2
    assert ledger.list_by_session_id.await_args.args[0] == sid
    assert ledger.list_by_session_id.await_args.kwargs["limit"] == 5


def test_list_by_session_400_on_bad_uuid() -> None:
    ledger = _mock_ledger()

    with patch(_LEDGER_PATH, return_value=ledger):
        with TestClient(_app(), raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/observations/route-traces/session/nope")

    assert resp.status_code == 400
    ledger.list_by_session_id.assert_not_awaited()


# ---------------------------------------------------------------------------
# GET /recent
# ---------------------------------------------------------------------------


def test_recent_returns_rows_no_filters() -> None:
    ledger = _mock_ledger()
    ledger.list_recent.return_value = [_row()]

    with patch(_LEDGER_PATH, return_value=ledger):
        with TestClient(_app(), raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/observations/route-traces/recent")

    assert resp.status_code == 200
    kwargs = ledger.list_recent.await_args.kwargs
    assert kwargs == {
        "limit": 50,
        "label_lie": False,
        "fallback_triggered": False,
        "not_reconciled": False,
    }


def test_recent_passes_filters_through() -> None:
    ledger = _mock_ledger()
    ledger.list_recent.return_value = []

    with patch(_LEDGER_PATH, return_value=ledger):
        with TestClient(_app(), raise_server_exceptions=True) as client:
            resp = client.get(
                "/api/v1/observations/route-traces/recent"
                "?label_lie=true&fallback_triggered=true&not_reconciled=true"
            )

    assert resp.status_code == 200
    kwargs = ledger.list_recent.await_args.kwargs
    assert kwargs["label_lie"] is True
    assert kwargs["fallback_triggered"] is True
    assert kwargs["not_reconciled"] is True


def test_recent_clamps_limit_to_max() -> None:
    ledger = _mock_ledger()
    ledger.list_recent.return_value = []

    with patch(_LEDGER_PATH, return_value=ledger):
        with TestClient(_app(), raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/observations/route-traces/recent?limit=1000000")

    assert resp.status_code == 200
    assert ledger.list_recent.await_args.kwargs["limit"] == 200


def test_recent_400_on_non_positive_limit() -> None:
    ledger = _mock_ledger()

    with patch(_LEDGER_PATH, return_value=ledger):
        with TestClient(_app(), raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/observations/route-traces/recent?limit=0")

    assert resp.status_code == 400
    ledger.list_recent.assert_not_awaited()


# ---------------------------------------------------------------------------
# 503 — ledger unconnected
# ---------------------------------------------------------------------------


def test_503_when_ledger_unconnected() -> None:
    ledger = AsyncMock()
    ledger.pool = None  # unconnected

    with patch(_LEDGER_PATH, return_value=ledger):
        with TestClient(_app(), raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/observations/route-traces/recent")

    assert resp.status_code == 503
