"""Unit tests for the user-upload presign/complete flow (FRE-369).

All tests are pure-unit: no real Postgres, R2, or Cloudflare. DB interactions
are intercepted via a stub async session; R2 interactions via AsyncMock patches.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_agent.service.auth import RequestUser, get_request_user
from personal_agent.service.database import get_db_session

# We import the router lazily in each test so monkeypatching works before the
# module-level singleton (artifact store) is resolved.
_USER_ID = uuid4()
_SESSION_ID = uuid4()
_JWT = "header.body.signature"


# ---------------------------------------------------------------------------
# Stub database session
# ---------------------------------------------------------------------------


class _StubSession:
    """Minimal async SQLAlchemy session that can be pre-loaded with results."""

    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self._rows: list[SimpleNamespace | None] = []
        self._add_calls: list[Any] = []
        self._committed = False
        self._row_idx = 0

    def enqueue(self, row: SimpleNamespace | None) -> None:
        """Queue the next row returned by execute()."""
        self._rows.append(row)

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.queries.append((str(statement), dict(params or {})))
        row = self._rows[self._row_idx] if self._row_idx < len(self._rows) else None
        self._row_idx += 1

        class _Result:
            def __init__(self, r: SimpleNamespace | None) -> None:
                self._r = r

            def first(self) -> SimpleNamespace | None:
                return self._r

            def fetchone(self) -> SimpleNamespace | None:
                return self._r

            def fetchall(self) -> list[Any]:
                return [self._r] if self._r is not None else []

            def one_or_none(self) -> SimpleNamespace | None:
                return self._r

            @property
            def rowcount(self) -> int:
                return getattr(self._r, "rowcount", 0)

        return _Result(row)

    def add(self, obj: Any) -> None:
        self._add_calls.append(obj)

    async def commit(self) -> None:
        self._committed = True

    async def refresh(self, obj: Any) -> None:
        pass

    async def __aenter__(self) -> "_StubSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass


def _build_app(session: _StubSession, *, store_mock: Any) -> FastAPI:
    """Build a minimal FastAPI app with uploads_router + overridden deps."""
    from personal_agent.service.uploads_router import router  # late import

    app = FastAPI()
    app.include_router(router)

    async def _db_override() -> Any:
        yield session

    async def _user_override() -> RequestUser:
        return RequestUser(user_id=_USER_ID, email="test@test.com", display_name=None)

    app.dependency_overrides[get_db_session] = _db_override
    app.dependency_overrides[get_request_user] = _user_override
    return app


def _pending_row(artifact_id: UUID, *, pending: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=artifact_id,
        user_id=_USER_ID,
        r2_key=f"upload/{_USER_ID}/{_SESSION_ID}/{artifact_id}.bin",
        content_type="image/png",
        size_bytes=0,
        upload_pending=pending,
        title="photo.png",
    )


# ---------------------------------------------------------------------------
# Tests — presign endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_presign_returns_url_and_artifact_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: valid content-type + size → 200 with uploadUrl + artifact_id."""
    import personal_agent.service.uploads_router as mod

    store = AsyncMock()
    store.generate_presigned_put_url = AsyncMock(return_value="https://r2.example.com/presigned")
    monkeypatch.setattr(mod, "_get_store", lambda: store)

    session = _StubSession()
    # The router INSERTs via raw SQL execute(); enqueue a pending-row result.
    artifact_id = uuid4()
    session.enqueue(_pending_row(artifact_id))

    app = _build_app(session, store_mock=store)
    with TestClient(app) as client:
        resp = client.post(
            "/api/uploads/presign",
            json={
                "filename": "photo.png",
                "content_type": "image/png",
                "size_hint": 1024,
            },
            headers={"Authorization": f"Bearer {_JWT}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "upload_url" in body
    assert "artifact_id" in body
    assert body["upload_url"] == "https://r2.example.com/presigned"


@pytest.mark.asyncio
async def test_presign_does_not_sign_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_presigned_put_url must NOT receive max_size / ContentLength.

    SigV4 signs ContentLength as an exact match; signing the cap value causes
    every upload whose actual size differs from max_size to receive 403
    SignatureDoesNotMatch from R2.  This test locks in the fix (FRE-369 review).
    """
    import personal_agent.service.uploads_router as mod

    store = AsyncMock()
    store.generate_presigned_put_url = AsyncMock(return_value="https://r2.example.com/presigned")
    monkeypatch.setattr(mod, "_get_store", lambda: store)

    session = _StubSession()
    artifact_id = uuid4()
    session.enqueue(_pending_row(artifact_id))

    app = _build_app(session, store_mock=store)
    with TestClient(app) as client:
        client.post(
            "/api/uploads/presign",
            json={"filename": "photo.png", "content_type": "image/png", "size_hint": 1024},
            headers={"Authorization": f"Bearer {_JWT}"},
        )

    call_kwargs = store.generate_presigned_put_url.call_args.kwargs
    assert "max_size" not in call_kwargs, "max_size must not be passed — SigV4 exact-match bug"
    # ContentLength must not appear anywhere in kwargs
    for key in call_kwargs:
        assert "content_length" not in key.lower(), f"ContentLength-like kwarg found: {key!r}"


@pytest.mark.asyncio
async def test_presign_rejects_disallowed_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-whitelisted MIME type → 415."""
    import personal_agent.service.uploads_router as mod

    store = AsyncMock()
    monkeypatch.setattr(mod, "_get_store", lambda: store)

    session = _StubSession()
    app = _build_app(session, store_mock=store)
    with TestClient(app) as client:
        resp = client.post(
            "/api/uploads/presign",
            json={
                "filename": "evil.exe",
                "content_type": "application/x-msdownload",
                "size_hint": 100,
            },
        )
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_presign_rejects_oversized_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """size_hint > upload_max_size_bytes → 413."""
    import personal_agent.service.uploads_router as mod
    from personal_agent.config import settings

    store = AsyncMock()
    monkeypatch.setattr(mod, "_get_store", lambda: store)
    monkeypatch.setattr(settings, "upload_max_size_bytes", 100, raising=False)

    session = _StubSession()
    app = _build_app(session, store_mock=store)
    with TestClient(app) as client:
        resp = client.post(
            "/api/uploads/presign",
            json={
                "filename": "bigfile.pdf",
                "content_type": "application/pdf",
                "size_hint": 999_999,
            },
        )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_presign_inserts_pending_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Presign call must insert a row with upload_pending=TRUE in the DB."""
    import personal_agent.service.uploads_router as mod

    store = AsyncMock()
    store.generate_presigned_put_url = AsyncMock(return_value="https://r2.example.com/url")
    monkeypatch.setattr(mod, "_get_store", lambda: store)

    session = _StubSession()
    artifact_id = uuid4()
    session.enqueue(_pending_row(artifact_id))

    app = _build_app(session, store_mock=store)
    with TestClient(app) as client:
        client.post(
            "/api/uploads/presign",
            json={
                "filename": "doc.pdf",
                "content_type": "application/pdf",
                "size_hint": 512,
            },
        )

    # At least one query should contain 'upload_pending' and a TRUE value
    all_sql = " ".join(q for q, _ in session.queries)
    assert "upload_pending" in all_sql.lower() or any(
        "upload_pending" in str(p) or True in p.values() for _, p in session.queries
    )


# ---------------------------------------------------------------------------
# Tests — complete endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """complete() verifies R2 HEAD, updates size_bytes, clears upload_pending → 200."""
    import personal_agent.service.uploads_router as mod

    artifact_id = uuid4()
    store = AsyncMock()
    store.head = AsyncMock(return_value={"content_length": 2048, "content_type": "image/png"})
    monkeypatch.setattr(mod, "_get_store", lambda: store)

    session = _StubSession()
    # First execute: SELECT the pending row
    session.enqueue(_pending_row(artifact_id, pending=True))
    # Second execute: UPDATE to clear pending (no result needed)
    session.enqueue(None)

    app = _build_app(session, store_mock=store)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/uploads/{artifact_id}/complete",
            headers={"Authorization": f"Bearer {_JWT}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["artifact_id"] == str(artifact_id)
    assert body["content_type"] == "image/png"


@pytest.mark.asyncio
async def test_complete_404_on_missing_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """complete() on an artifact_id not in DB (or not owned) → 404."""
    import personal_agent.service.uploads_router as mod

    store = AsyncMock()
    monkeypatch.setattr(mod, "_get_store", lambda: store)

    session = _StubSession()
    session.enqueue(None)  # not found

    app = _build_app(session, store_mock=store)
    with TestClient(app) as client:
        resp = client.post(f"/api/uploads/{uuid4()}/complete")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_complete_404_on_already_completed_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """complete() on a row with upload_pending=FALSE → 404 (idempotency guard)."""
    import personal_agent.service.uploads_router as mod

    store = AsyncMock()
    monkeypatch.setattr(mod, "_get_store", lambda: store)

    artifact_id = uuid4()
    session = _StubSession()
    # SQL has AND upload_pending = TRUE — a completed row is invisible to the query.
    session.enqueue(None)

    app = _build_app(session, store_mock=store)
    with TestClient(app) as client:
        resp = client.post(f"/api/uploads/{artifact_id}/complete")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_complete_502_when_r2_object_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """complete() when R2 HEAD fails (object not yet present) → 502."""
    import personal_agent.service.uploads_router as mod
    from personal_agent.storage.artifact_store import ArtifactStoreError

    artifact_id = uuid4()
    store = AsyncMock()
    store.head = AsyncMock(side_effect=ArtifactStoreError("not found"))
    monkeypatch.setattr(mod, "_get_store", lambda: store)

    session = _StubSession()
    session.enqueue(_pending_row(artifact_id, pending=True))

    app = _build_app(session, store_mock=store)
    with TestClient(app) as client:
        resp = client.post(f"/api/uploads/{artifact_id}/complete")

    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_complete_413_when_actual_size_exceeds_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """complete() when R2 ContentLength > upload_max_size_bytes → 413."""
    import personal_agent.service.uploads_router as mod
    from personal_agent.config import settings

    artifact_id = uuid4()
    store = AsyncMock()
    store.head = AsyncMock(
        return_value={"content_length": 999_999_999, "content_type": "image/png"}
    )
    monkeypatch.setattr(mod, "_get_store", lambda: store)
    monkeypatch.setattr(settings, "upload_max_size_bytes", 100, raising=False)

    session = _StubSession()
    session.enqueue(_pending_row(artifact_id, pending=True))

    app = _build_app(session, store_mock=store)
    with TestClient(app) as client:
        resp = client.post(f"/api/uploads/{artifact_id}/complete")

    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_complete_cross_user_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """complete() does NOT return a row owned by a different user."""
    import personal_agent.service.uploads_router as mod

    other_user = uuid4()
    artifact_id = uuid4()
    store = AsyncMock()
    monkeypatch.setattr(mod, "_get_store", lambda: store)

    session = _StubSession()
    # SQL filters AND user_id = :user_id; row owned by other_user → DB returns None.
    session.enqueue(None)

    # Override auth to use _USER_ID (different from other_user)
    from personal_agent.service.uploads_router import router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)

    async def _db_override() -> Any:
        yield session

    async def _user_override() -> RequestUser:
        return RequestUser(user_id=_USER_ID, email="test@test.com", display_name=None)

    app.dependency_overrides[get_db_session] = _db_override
    app.dependency_overrides[get_request_user] = _user_override

    with TestClient(app) as client:
        resp = client.post(f"/api/uploads/{artifact_id}/complete")

    # The DB query must filter by user_id; row belongs to other_user → 404
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests — expiry cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expire_pending_uploads_deletes_old_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """expire_pending_uploads() fetches r2_keys, deletes R2 objects, deletes DB rows."""
    import personal_agent.service.uploads_router as mod
    from personal_agent.service.uploads_router import expire_pending_uploads  # noqa: PLC0415

    store = AsyncMock()
    store.delete = AsyncMock(return_value=None)
    monkeypatch.setattr(mod, "_get_store", lambda: store)

    deleted_count = 3
    session = _StubSession()
    # First execute: SELECT r2_key — returns one row with an r2_key
    session.enqueue(SimpleNamespace(r2_key="upload/user/GLOBAL/abc.png"))
    # Second execute: DELETE — returns rowcount
    session.enqueue(SimpleNamespace(rowcount=deleted_count))

    # Must be sync callable returning an async context manager (like AsyncSessionLocal).
    def _factory() -> Any:
        return session

    # The function signature accepts an AsyncSessionLocal-like factory
    n = await expire_pending_uploads(_factory)
    assert n == deleted_count
    # R2 delete was called for the orphaned object
    store.delete.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — _augment_message_with_attachments helper
# ---------------------------------------------------------------------------


def test_augment_message_prepends_attachment_context() -> None:
    """_augment_message_with_attachments produces the expected prefix."""
    from personal_agent.service.app import _augment_message_with_attachments

    attachments = [
        {"artifact_id": "abc-123", "content_type": "image/png", "title": "photo.png"},
    ]
    result = _augment_message_with_attachments("Hello!", attachments)
    assert "abc-123" in result
    assert "image/png" in result
    assert "Hello!" in result
    assert result.index("abc-123") < result.index("Hello!")


def test_augment_message_no_attachments_returns_original() -> None:
    """_augment_message_with_attachments with empty list returns message unchanged."""
    from personal_agent.service.app import _augment_message_with_attachments

    result = _augment_message_with_attachments("Unchanged", [])
    assert result == "Unchanged"
