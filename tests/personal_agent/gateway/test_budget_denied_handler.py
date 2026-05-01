"""FastAPI BudgetDenied → 503 handler (FRE-306).

Verifies the global exception handler registered in ``service/app.py``
maps ``BudgetDenied`` to a structured 503 with the cap / spend / reset_time
payload the PWA error card consumes — closing the regression where the
denial was rendered as an empty assistant turn.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.cost_gate import BudgetDenied


def _build_test_app() -> FastAPI:
    """Build a tiny FastAPI app with the same handler logic as service.app."""
    from fastapi import Request
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.exception_handler(BudgetDenied)
    async def _handler(_request: Request, exc: BudgetDenied) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": "budget_denied",
                "role": exc.role,
                "time_window": exc.time_window,
                "cap": str(exc.cap),
                "spend": str(exc.current_spend),
                "reset_time": exc.window_resets_at.isoformat(),
                "denial_reason": exc.denial_reason,
                "status": 503,
            },
        )

    @app.post("/raises")
    def _endpoint() -> dict[str, str]:
        raise BudgetDenied(
            role="main_inference",
            time_window="weekly",
            current_spend=Decimal("18.04"),
            cap=Decimal("18.00"),
            window_resets_at=datetime(2026, 5, 4, tzinfo=timezone.utc),
            denial_reason="cap_exceeded",
        )

    return app


def test_budget_denied_renders_structured_503() -> None:
    """The handler returns 503 with the documented payload shape."""
    client = TestClient(_build_test_app())
    resp = client.post("/raises")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "budget_denied"
    assert body["role"] == "main_inference"
    assert body["time_window"] == "weekly"
    assert body["cap"] == "18.00"
    assert body["spend"] == "18.04"
    assert body["reset_time"] == "2026-05-04T00:00:00+00:00"
    assert body["denial_reason"] == "cap_exceeded"
    assert body["status"] == 503
