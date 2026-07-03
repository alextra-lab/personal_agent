"""Unit tests for the R2 artifact store (ADR-0069 / FRE-227)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from personal_agent.storage.artifact_store import (
    ALLOWED_ARTIFACT_TYPES,
    ArtifactKeyError,
    ArtifactStoreError,
    R2ArtifactStore,
    build_r2_key,
)

# ---------------------------------------------------------------------------
# build_r2_key
# ---------------------------------------------------------------------------


_USER = UUID("11111111-1111-1111-1111-111111111111")
_SESSION = UUID("22222222-2222-2222-2222-222222222222")
_ART = UUID("33333333-3333-3333-3333-333333333333")


def test_build_r2_key_happy_path() -> None:
    """Layout follows {type}/{user_id}/{session_id}/{artifact_id}_{slug}.{ext}."""
    key = build_r2_key(
        type="note",
        user_id=_USER,
        session_id=_SESSION,
        artifact_id=_ART,
        slug="release-notes",
        ext="md",
    )
    expected = f"note/{_USER}/{_SESSION}/{_ART}_release-notes.md"
    assert key == expected


def test_build_r2_key_session_null_renders_global() -> None:
    """``session_id=None`` produces the literal ``GLOBAL`` segment."""
    key = build_r2_key(
        type="artifact",
        user_id=_USER,
        session_id=None,
        artifact_id=_ART,
        slug=None,
        ext="html",
    )
    assert key == f"artifact/{_USER}/GLOBAL/{_ART}.html"


def test_build_r2_key_omits_slug_when_none() -> None:
    """No trailing ``_`` separator when slug is absent."""
    key = build_r2_key(
        type="note",
        user_id=_USER,
        session_id=_SESSION,
        artifact_id=_ART,
        slug=None,
        ext="md",
    )
    assert key == f"note/{_USER}/{_SESSION}/{_ART}.md"


@pytest.mark.parametrize("bad_slug", ["..", "../etc/passwd", "foo/bar"])
def test_build_r2_key_rejects_traversal_slug(bad_slug: str) -> None:
    """Slashes and traversal segments are bounced before any R2 call."""
    with pytest.raises(ArtifactKeyError):
        build_r2_key(
            type="note",
            user_id=_USER,
            session_id=_SESSION,
            artifact_id=_ART,
            slug=bad_slug,
            ext="md",
        )


@pytest.mark.parametrize("bad_slug", ["-leading-dash", ".dotfile", "", " "])
def test_build_r2_key_rejects_disallowed_leading_chars(bad_slug: str) -> None:
    """Slugs must start alphanumeric — leading dashes / dots / empty rejected."""
    with pytest.raises(ArtifactKeyError):
        build_r2_key(
            type="note",
            user_id=_USER,
            session_id=_SESSION,
            artifact_id=_ART,
            slug=bad_slug,
            ext="md",
        )


def test_build_r2_key_rejects_control_chars() -> None:
    """Newlines / NUL bytes cannot reach R2 keys."""
    with pytest.raises(ArtifactKeyError):
        build_r2_key(
            type="note",
            user_id=_USER,
            session_id=_SESSION,
            artifact_id=_ART,
            slug="bad\x00slug",
            ext="md",
        )


def test_build_r2_key_rejects_disallowed_type() -> None:
    """Type must come from the allowed enum."""
    with pytest.raises(ArtifactKeyError):
        build_r2_key(
            type="system",  # type: ignore[arg-type]
            user_id=_USER,
            session_id=_SESSION,
            artifact_id=_ART,
            slug=None,
            ext="md",
        )


def test_build_r2_key_rejects_disallowed_ext() -> None:
    """Extension must be short lowercase alnum — bounced if it has a slash."""
    with pytest.raises(ArtifactKeyError):
        build_r2_key(
            type="note",
            user_id=_USER,
            session_id=_SESSION,
            artifact_id=_ART,
            slug=None,
            ext="md/oops",
        )


def test_allowed_types_match_check_constraint() -> None:
    """If this fires, the SQL CHECK and the Python guard have drifted apart."""
    assert ALLOWED_ARTIFACT_TYPES == {"note", "artifact", "upload", "capture"}


# ---------------------------------------------------------------------------
# R2ArtifactStore (mocked aiobotocore)
# ---------------------------------------------------------------------------


class _FakeS3Client:
    """Minimal async stand-in for the aiobotocore S3 client."""

    def __init__(self) -> None:
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.presign_calls: list[dict[str, Any]] = []
        self._payloads: dict[str, bytes] = {}

    def stash(self, key: str, payload: bytes) -> None:
        self._payloads[key] = payload

    async def put_object(self, **kwargs: Any) -> None:
        self.put_calls.append(kwargs)
        self._payloads[kwargs["Key"]] = kwargs["Body"]

    async def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls.append(kwargs)
        body = AsyncMock()
        body.read = AsyncMock(return_value=self._payloads.get(kwargs["Key"], b""))
        body.close = MagicMock()
        return {"Body": body}

    async def delete_object(self, **kwargs: Any) -> None:
        self.delete_calls.append(kwargs)
        self._payloads.pop(kwargs["Key"], None)

    async def generate_presigned_url(self, op: str, **kwargs: Any) -> str:
        self.presign_calls.append({"op": op, **kwargs})
        return f"https://presigned.example/{kwargs['Params']['Key']}"


class _FakeSession:
    """Fakes ``aiobotocore.session.AioSession``."""

    def __init__(self, client: _FakeS3Client) -> None:
        self._client = client

    def create_client(self, *_args: Any, **kwargs: Any) -> Any:
        # ``aiobotocore`` returns an async context manager.
        client = self._client
        client.create_kwargs = kwargs  # type: ignore[attr-defined]

        class _Ctx:
            async def __aenter__(self_inner) -> _FakeS3Client:
                return client

            async def __aexit__(self_inner, *exc: object) -> None:
                return None

        return _Ctx()


def _store_with(fake: _FakeS3Client) -> R2ArtifactStore:
    store = R2ArtifactStore(
        endpoint_url="https://r2.test.local",
        bucket="artifacts-test",
        access_key_id="ak",
        secret_access_key="sk",
        region="auto",
    )
    store._session = _FakeSession(fake)  # type: ignore[assignment]
    return store


@pytest.mark.asyncio
async def test_put_propagates_content_type_and_metadata() -> None:
    fake = _FakeS3Client()
    store = _store_with(fake)

    await store.put(
        r2_key="note/aaa/GLOBAL/bbb.md",
        content=b"hello",
        content_type="text/markdown; charset=utf-8",
        metadata={"trace_id": "abc"},
    )
    await store.aclose()

    assert fake.put_calls == [
        {
            "Bucket": "artifacts-test",
            "Key": "note/aaa/GLOBAL/bbb.md",
            "Body": b"hello",
            "ContentType": "text/markdown; charset=utf-8",
            "Metadata": {"trace_id": "abc"},
        }
    ]
    # The create_client kwargs propagate the constructor settings exactly.
    assert fake.create_kwargs["endpoint_url"] == "https://r2.test.local"  # type: ignore[attr-defined]
    assert fake.create_kwargs["region_name"] == "auto"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_returns_stored_bytes() -> None:
    fake = _FakeS3Client()
    fake.stash("k", b"payload")
    store = _store_with(fake)

    out = await store.get("k")

    assert out == b"payload"


@pytest.mark.asyncio
async def test_get_raises_artifact_store_error_on_botocore_failure() -> None:
    """``ClientError`` is wrapped in ``ArtifactStoreError`` for callers."""
    from botocore.exceptions import ClientError

    fake = _FakeS3Client()

    async def raise_client_error(**_kwargs: Any) -> dict[str, Any]:
        raise ClientError(
            error_response={"Error": {"Code": "NoSuchKey", "Message": "missing"}},
            operation_name="GetObject",
        )

    fake.get_object = raise_client_error  # type: ignore[assignment]
    store = _store_with(fake)

    with pytest.raises(ArtifactStoreError):
        await store.get("missing-key")


@pytest.mark.asyncio
async def test_get_failure_log_carries_session_and_task_id() -> None:
    """FRE-693 (ADR-0074 §8c): the byte-fetch failure log threads session_id/task_id."""
    import structlog
    from botocore.exceptions import ClientError

    fake = _FakeS3Client()

    async def raise_client_error(**_kwargs: Any) -> dict[str, Any]:
        raise ClientError(
            error_response={"Error": {"Code": "NoSuchKey", "Message": "missing"}},
            operation_name="GetObject",
        )

    fake.get_object = raise_client_error  # type: ignore[assignment]
    store = _store_with(fake)

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(ArtifactStoreError):
            await store.get(
                "missing-key", trace_id="trace-1", session_id="sess-1", task_id="task-1"
            )

    failure_logs = [entry for entry in logs if entry.get("event") == "artifact_store_get_failed"]
    assert failure_logs, f"artifact_store_get_failed not found in: {logs}"
    assert failure_logs[0]["session_id"] == "sess-1"
    assert failure_logs[0]["task_id"] == "task-1"


@pytest.mark.asyncio
async def test_delete_is_idempotent_via_client() -> None:
    fake = _FakeS3Client()
    fake.stash("k", b"present")
    store = _store_with(fake)

    await store.delete("k")
    await store.delete("k")  # Second call hits a missing key but the fake accepts it.

    assert len(fake.delete_calls) == 2
    assert fake.delete_calls[0]["Key"] == "k"


@pytest.mark.asyncio
async def test_presigned_put_url_does_not_sign_content_length() -> None:
    """ContentLength must NOT appear in the SigV4-signed Params.

    SigV4 treats ContentLength as an exact match: signing max_size means every
    upload whose actual size differs from max_size fails with 403
    SignatureDoesNotMatch.  Size enforcement belongs at /complete (HEAD check).
    """
    fake = _FakeS3Client()
    store = _store_with(fake)

    url = await store.generate_presigned_put_url(
        r2_key="upload/u/GLOBAL/a.png",
        content_type="image/png",
        expires_in=600,
    )

    assert url.startswith("https://presigned.example/")
    assert fake.presign_calls[0]["op"] == "put_object"
    params = fake.presign_calls[0]["Params"]
    assert params["Bucket"] == "artifacts-test"
    assert params["Key"] == "upload/u/GLOBAL/a.png"
    assert params["ContentType"] == "image/png"
    assert "ContentLength" not in params, "ContentLength must not be signed (SigV4 exact-match bug)"
    assert fake.presign_calls[0]["ExpiresIn"] == 600
    assert fake.presign_calls[0]["HttpMethod"] == "PUT"


@pytest.mark.asyncio
async def test_aclose_is_idempotent() -> None:
    fake = _FakeS3Client()
    store = _store_with(fake)
    await store._get_client()
    await store.aclose()
    await store.aclose()  # no-op the second time


@pytest.mark.asyncio
async def test_context_manager_opens_and_closes() -> None:
    fake = _FakeS3Client()
    store = _store_with(fake)

    async with store as s:
        await s.put(
            r2_key="note/u/s/a.md",
            content=b"x",
            content_type="text/markdown; charset=utf-8",
        )

    assert len(fake.put_calls) == 1


@pytest.mark.asyncio
async def test_get_artifact_store_returns_none_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Substrate gate: missing env vars => no singleton."""
    import personal_agent.storage.artifact_store as mod

    monkeypatch.setattr(mod, "_singleton", None)
    monkeypatch.setattr(mod.settings, "r2_endpoint_url", None, raising=False)
    monkeypatch.setattr(mod.settings, "r2_access_key_id", None, raising=False)
    monkeypatch.setattr(mod.settings, "r2_secret_access_key", None, raising=False)

    assert mod.get_artifact_store() is None


@pytest.mark.asyncio
async def test_get_artifact_store_returns_singleton_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import personal_agent.storage.artifact_store as mod

    monkeypatch.setattr(mod, "_singleton", None)
    monkeypatch.setattr(mod.settings, "r2_endpoint_url", "https://r2.test", raising=False)
    monkeypatch.setattr(mod.settings, "r2_bucket_name", "artifacts-x", raising=False)
    monkeypatch.setattr(mod.settings, "r2_access_key_id", "ak", raising=False)
    monkeypatch.setattr(mod.settings, "r2_secret_access_key", "sk", raising=False)
    monkeypatch.setattr(mod.settings, "r2_region", "auto", raising=False)

    first = mod.get_artifact_store()
    second = mod.get_artifact_store()
    try:
        assert first is not None
        assert first is second
        assert first.bucket == "artifacts-x"
    finally:
        monkeypatch.setattr(mod, "_singleton", None)
