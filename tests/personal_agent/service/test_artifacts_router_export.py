"""Unit tests for GET /api/v1/artifacts/{id}/export (FRE-530, ADR-0089 A5).

A minimal FastAPI app with the DB + identity dependencies overridden and the R2
store + asset fetcher monkeypatched — no real Postgres, R2, CDN, or Worker. The
transform itself is covered by ``tests/personal_agent/storage/test_artifact_export.py``;
these tests assert the endpoint's plumbing: ownership, content-type gating, mode
selection, attachment disposition, and error mapping.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.service import artifacts_router as router_module
from personal_agent.service.artifacts_router import router
from personal_agent.service.auth import RequestUser, get_request_user
from personal_agent.service.database import get_db_session
from personal_agent.storage.artifact_export import ArtifactExportError

_ORIGIN = "https://artifacts.frenchforet.com"
_OWNER = uuid4()


class _StubSession:
    def __init__(self, *, found: SimpleNamespace | None) -> None:
        self._found = found

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        return SimpleNamespace(one_or_none=lambda: self._found)


class _FakeStore:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def get(self, r2_key: str, *, trace_id: str | None = None) -> bytes:
        return self._payload


class _RaisingFetcher:
    async def fetch(self, url: str) -> bytes:
        raise ArtifactExportError(f"origin unreachable: {url}")


def _row(*, content_type: str = "text/html; charset=utf-8") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        slug="my-report",
        title="My Report",
        summary=None,
        content_type=content_type,
        size_bytes=10,
        r2_key="artifact/x/y/z.html",
        tags=[],
        created_at=None,
    )


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    row: SimpleNamespace | None,
    payload: bytes = b"<html></html>",
    fetcher: Any = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    async def _override_db() -> Any:
        yield _StubSession(found=row)

    def _override_user() -> RequestUser:
        return RequestUser(user_id=_OWNER, email="owner@example.com")

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_request_user] = _override_user

    monkeypatch.setattr(router_module, "get_artifact_store", lambda: _FakeStore(payload))
    if fetcher is not None:
        monkeypatch.setattr(router_module, "_build_asset_fetcher", lambda timeout=None: fetcher)
    return TestClient(app)


def test_export_inline_returns_attachment(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, row=_row(), payload=b"<html><body>hi</body></html>")
    resp = client.get(f"/api/v1/artifacts/{uuid4()}/export?mode=inline")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert "my-report.html" in resp.headers["content-disposition"]
    assert resp.headers["x-artifact-export-mode"] == "inline"
    assert "hi" in resp.text


def test_export_substitute_rewrites_to_cdn(monkeypatch: pytest.MonkeyPatch) -> None:
    html = (
        "<html><head>"
        f'<script src="{_ORIGIN}/lib/chartjs@4.4.7/chart.umd.js"></script>'
        "</head><body></body></html>"
    ).encode()
    client = _client(monkeypatch, row=_row(), payload=html)
    resp = client.get(f"/api/v1/artifacts/{uuid4()}/export?mode=substitute")
    assert resp.status_code == 200
    assert "cdn.jsdelivr.net" in resp.text
    assert "integrity=" in resp.text
    assert _ORIGIN not in resp.text


def test_export_defaults_to_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, row=_row(), payload=b"<html></html>")
    resp = client.get(f"/api/v1/artifacts/{uuid4()}/export")
    assert resp.status_code == 200
    assert resp.headers["x-artifact-export-mode"] == "inline"


def test_export_non_html_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, row=_row(content_type="text/markdown; charset=utf-8"))
    resp = client.get(f"/api/v1/artifacts/{uuid4()}/export")
    assert resp.status_code == 400


def test_export_missing_or_cross_user_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, row=None)
    resp = client.get(f"/api/v1/artifacts/{uuid4()}/export")
    assert resp.status_code == 404


def test_export_bad_mode_422(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, row=_row())
    resp = client.get(f"/api/v1/artifacts/{uuid4()}/export?mode=bogus")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_http_fetcher_rejects_disallowed_host() -> None:
    # SSRF guard: a host outside the allowlist is refused before any request.
    fetcher = router_module._HttpAssetFetcher(
        origin_host="artifacts.frenchforet.com",
        allowed_hosts=frozenset({"artifacts.frenchforet.com", "cdn.jsdelivr.net"}),
    )
    with pytest.raises(ArtifactExportError):
        await fetcher.fetch("https://169.254.169.254/latest/meta-data/")


def test_build_asset_fetcher_allowlist_from_map() -> None:
    from personal_agent.storage.artifact_export import load_substitution_map

    fetcher = router_module._build_asset_fetcher(load_substitution_map())
    assert isinstance(fetcher, router_module._HttpAssetFetcher)
    assert "artifacts.frenchforet.com" in fetcher._allowed_hosts
    assert "cdn.jsdelivr.net" in fetcher._allowed_hosts


def test_export_fetch_failure_502(monkeypatch: pytest.MonkeyPatch) -> None:
    # inline export of an inline-only asset (three.js) forces a fetch
    html = f'<script src="{_ORIGIN}/lib/three@0.171.0/three.iife.min.js"></script>'.encode()
    client = _client(monkeypatch, row=_row(), payload=html, fetcher=_RaisingFetcher())
    resp = client.get(f"/api/v1/artifacts/{uuid4()}/export?mode=inline")
    assert resp.status_code == 502
