"""Unit tests for GET /telemetry/effective-config (ADR-0129 D5, FRE-1070 AC-3).

Parametrized over two distinct ``otel_exporter_endpoint`` settings values to prove the
endpoint genuinely reflects resolved runtime configuration rather than a hardcoded string.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.config import settings
from personal_agent.service.telemetry_router import router


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.parametrize(
    "endpoint",
    ["localhost:4319", "otel-collector:4317"],
    ids=["dev-default-shape", "cloud-compose-shape"],
)
def test_effective_config_reflects_resolved_endpoint(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setattr(settings, "otel_exporter_endpoint", endpoint)

    with TestClient(_build_app()) as client:
        resp = client.get("/telemetry/effective-config")

    assert resp.status_code == 200
    body = resp.json()
    assert body["otlp_endpoint"] == endpoint
    assert body["otlp_protocol"] == "grpc"
    assert body["insecure"] is True
    assert body["service_name"]
