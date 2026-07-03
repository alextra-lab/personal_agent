"""R2-backed artifact store (ADR-0069 / FRE-227).

Async wrapper around ``aiobotocore`` that uploads, fetches, and deletes
bytes in Cloudflare R2 (or any S3-compatible store). Callers must already
have computed an ``artifact_id`` and decided the artifact ``type`` /
``slug``; this module owns the R2 key layout and the S3 protocol details.

Design notes
------------
* R2 keys are hierarchical (``{type}/{user_id}/{session_id|GLOBAL}/...``)
  so the R2 console remains human-greppable per ADR-0069 D5. Public URLs
  are flat (``{artifacts_public_base_url}/{artifact_id}``) — the Worker
  resolves them via the gateway's ``/internal/artifacts/{id}`` endpoint.
* ``build_r2_key`` is a strict validator: any slug/type/extension input
  outside its grammar raises ``ArtifactKeyError`` *before* a network call
  reaches R2. This is the substrate's primary prefix-escape guard.
* The S3 client is reused across requests via ``_get_client``. A FastAPI
  lifespan hook calls ``aclose`` on shutdown; in tests we use the async
  context manager form.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar, cast
from uuid import UUID

from aiobotocore.session import AioSession  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from personal_agent.config import settings
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


# Artifact `type` discriminator — kept in lock-step with the CHECK
# constraint on docker/postgres/init.sql `artifacts.type` and the migration
# in docker/postgres/migrations/0003_artifacts_schema.sql.
ALLOWED_ARTIFACT_TYPES: frozenset[str] = frozenset({"note", "artifact", "upload", "capture"})

# Slug grammar: alphanumeric start, then alnum / `.` / `_` / `-`, max 64
# chars. Rejects empty, traversal (`..`), slashes, control characters, and
# leading dashes/dots — none of which should ever reach an R2 key.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Extension grammar: lowercase alnum, max 8 chars. Common extensions
# (`md`, `txt`, `html`, `pdf`, `png`, etc.) all pass.
_EXT_RE = re.compile(r"^[a-z0-9]{1,8}$")


class ArtifactKeyError(ValueError):
    """Raised when an artifact-key input fails validation.

    Always raised before any network or DB call so that traversal attempts
    cannot allocate UUIDs or burn embedding budget.
    """


class ArtifactStoreError(RuntimeError):
    """Wraps ``botocore`` / network errors raised by R2 operations."""


def build_r2_key(
    *,
    type: str,
    user_id: UUID,
    session_id: UUID | None,
    artifact_id: UUID,
    slug: str | None,
    ext: str,
) -> str:
    """Produce a hierarchical R2 key per ADR-0069 D5.

    Layout: ``{type}/{user_id}/{session_id|GLOBAL}/{artifact_id}[_{slug}].{ext}``

    Args:
        type: One of ``ALLOWED_ARTIFACT_TYPES``.
        user_id: Owner UUID.
        session_id: Producing session (``GLOBAL`` segment when ``None``).
        artifact_id: The artifact's UUID.
        slug: Optional human-readable handle; appended after the id with a
            ``_`` separator. Must match ``_SLUG_RE``.
        ext: Filename extension (no leading dot).

    Returns:
        The opaque R2 key string. Never exposed to callers; the public URL
        is built from ``artifacts_public_base_url`` + ``artifact_id``.

    Raises:
        ArtifactKeyError: When any input is malformed.
    """
    if type not in ALLOWED_ARTIFACT_TYPES:
        raise ArtifactKeyError(f"artifact type {type!r} not in {sorted(ALLOWED_ARTIFACT_TYPES)}")

    if not _EXT_RE.match(ext):
        raise ArtifactKeyError(f"extension {ext!r} must match {_EXT_RE.pattern!r}")

    if slug is not None and not _SLUG_RE.match(slug):
        raise ArtifactKeyError(f"slug {slug!r} must match {_SLUG_RE.pattern!r}")

    session_segment = "GLOBAL" if session_id is None else str(session_id)
    if slug:
        filename = f"{artifact_id}_{slug}.{ext}"
    else:
        filename = f"{artifact_id}.{ext}"

    return f"{type}/{user_id}/{session_segment}/{filename}"


class R2ArtifactStore:
    """Async R2 / S3 wrapper. Reuse a single instance per process.

    Use either as an async context manager (closes the underlying client
    on ``__aexit__``) or hold a long-lived instance and call :meth:`aclose`
    explicitly on shutdown.
    """

    ALLOWED_TYPES: ClassVar[frozenset[str]] = ALLOWED_ARTIFACT_TYPES

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "auto",
    ) -> None:
        """Construct the store. Credentials are held in process memory only."""
        self._endpoint_url = endpoint_url
        self._bucket = bucket
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._region = region
        self._session: Any = AioSession()
        # Lazily opened on first use, reused thereafter.
        self._client: Any = None
        # ``aiobotocore`` ``create_client`` returns an async context manager,
        # not a bare client. We keep the context object so ``aclose`` can
        # cleanly invoke ``__aexit__``.
        self._client_ctx: Any = None

    @property
    def bucket(self) -> str:
        """Name of the R2 bucket this store reads/writes."""
        return self._bucket

    async def __aenter__(self) -> R2ArtifactStore:
        """Open the S3 client and return self for the ``async with`` block."""
        await self._get_client()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the S3 client when leaving the ``async with`` block."""
        await self.aclose()

    async def _get_client(self) -> Any:
        if self._client is None:
            self._client_ctx = self._session.create_client(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._access_key_id,
                aws_secret_access_key=self._secret_access_key,
                region_name=self._region,
            )
            assert self._client_ctx is not None
            self._client = await self._client_ctx.__aenter__()
        return self._client

    async def aclose(self) -> None:
        """Close the underlying S3 client. Safe to call multiple times."""
        if self._client_ctx is not None:
            try:
                await self._client_ctx.__aexit__(None, None, None)
            finally:
                self._client = None
                self._client_ctx = None

    async def put(
        self,
        *,
        r2_key: str,
        content: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
        trace_id: str | None = None,
    ) -> None:
        """Upload bytes to R2 at ``r2_key`` with the given content-type.

        Args:
            r2_key: Destination object key in the bucket.
            content: Bytes to upload.
            content_type: MIME type to set on the object.
            metadata: Optional R2 user metadata to attach.
            trace_id: Originating request trace_id, threaded onto failure logs
                for §I3 identity threading.

        Raises:
            ArtifactStoreError: On ``ClientError`` / ``BotoCoreError``.
        """
        client = await self._get_client()
        try:
            await client.put_object(
                Bucket=self._bucket,
                Key=r2_key,
                Body=content,
                ContentType=content_type,
                Metadata=dict(metadata or {}),
            )
        except (ClientError, BotoCoreError) as exc:
            log.error(
                "artifact_store_put_failed",
                bucket=self._bucket,
                r2_key=r2_key,
                error=str(exc),
                trace_id=trace_id,
            )
            raise ArtifactStoreError(f"R2 put failed for {r2_key}: {exc}") from exc

    async def get(
        self,
        r2_key: str,
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> bytes:
        """Fetch the bytes at ``r2_key`` from R2.

        Args:
            r2_key: Object key to fetch from the bucket.
            trace_id: Originating request trace_id, threaded onto failure logs
                for §I3 identity threading.
            session_id: Originating session id, threaded onto failure logs
                for §8c joinability (FRE-693).
            task_id: Sub-agent task id, threaded onto failure logs for §8c
                joinability (FRE-693) — ``None`` at the turn level.

        Raises:
            ArtifactStoreError: On ``ClientError`` / ``BotoCoreError``.
        """
        client = await self._get_client()
        try:
            response = await client.get_object(Bucket=self._bucket, Key=r2_key)
            body = response["Body"]
            try:
                return cast(bytes, await body.read())
            finally:
                close = getattr(body, "close", None)
                if close is not None:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
        except (ClientError, BotoCoreError) as exc:
            log.error(
                "artifact_store_get_failed",
                bucket=self._bucket,
                r2_key=r2_key,
                error=str(exc),
                trace_id=trace_id,
                session_id=session_id,
                task_id=task_id,
            )
            raise ArtifactStoreError(f"R2 get failed for {r2_key}: {exc}") from exc

    async def delete(self, r2_key: str, *, trace_id: str | None = None) -> None:
        """Delete the object at ``r2_key`` from R2.

        Idempotent — deleting a non-existent key is not an error.

        Args:
            r2_key: Object key to delete.
            trace_id: Originating request trace_id, threaded onto failure logs
                for §I3 identity threading.
        """
        client = await self._get_client()
        try:
            await client.delete_object(Bucket=self._bucket, Key=r2_key)
        except (ClientError, BotoCoreError) as exc:
            log.error(
                "artifact_store_delete_failed",
                bucket=self._bucket,
                r2_key=r2_key,
                error=str(exc),
                trace_id=trace_id,
            )
            raise ArtifactStoreError(f"R2 delete failed for {r2_key}: {exc}") from exc

    async def head(self, r2_key: str, *, trace_id: str | None = None) -> dict[str, object]:
        """Return HEAD metadata for the object at ``r2_key``.

        Args:
            r2_key: Object key to inspect.
            trace_id: Originating request trace_id for failure logs.

        Returns:
            Dict with at least ``content_length: int`` and ``content_type: str``.

        Raises:
            ArtifactStoreError: When the object does not exist or on network error.
        """
        client = await self._get_client()
        try:
            response = await client.head_object(Bucket=self._bucket, Key=r2_key)
            return {
                "content_length": response.get("ContentLength", 0),
                "content_type": response.get("ContentType", ""),
            }
        except (ClientError, BotoCoreError) as exc:
            log.info(
                "artifact_store_head_failed",
                bucket=self._bucket,
                r2_key=r2_key,
                error=str(exc),
                trace_id=trace_id,
            )
            raise ArtifactStoreError(f"R2 head failed for {r2_key}: {exc}") from exc

    async def generate_presigned_put_url(
        self,
        *,
        r2_key: str,
        content_type: str,
        expires_in: int = 900,
        trace_id: str | None = None,
    ) -> str:
        """Mint a presigned PUT URL the browser can upload to directly.

        Used by FRE-369's user-upload flow. The presigned URL embeds the
        bucket, key, and required content-type. Size enforcement is done
        server-side by ``/complete`` via a HEAD check — ContentLength is NOT
        signed because SigV4 treats it as an exact-match, which would cause
        every upload whose size differs from the signed value to fail 403.

        Args:
            r2_key: Destination object key.
            content_type: Required MIME type the uploader must use.
            expires_in: URL lifetime in seconds.
            trace_id: Originating request trace_id, threaded onto failure logs
                for §I3 identity threading.

        Raises:
            ArtifactStoreError: On ``ClientError`` / ``BotoCoreError``.
        """
        client = await self._get_client()
        try:
            url: str = await client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": r2_key,
                    "ContentType": content_type,
                },
                ExpiresIn=expires_in,
                HttpMethod="PUT",
            )
            return url
        except (ClientError, BotoCoreError) as exc:
            log.error(
                "artifact_store_presign_failed",
                bucket=self._bucket,
                r2_key=r2_key,
                error=str(exc),
                trace_id=trace_id,
            )
            raise ArtifactStoreError(f"R2 presign failed for {r2_key}: {exc}") from exc


# Module-level singleton accessor. Constructed lazily so importing
# personal_agent.storage in environments without R2 credentials is cheap.
_singleton: R2ArtifactStore | None = None


def get_artifact_store() -> R2ArtifactStore | None:
    """Return the process-wide R2 artifact store, or ``None`` if unwired.

    Callers must handle the ``None`` case gracefully (the substrate is
    optional; the notes_* tools register only when this returns a store).
    """
    global _singleton
    if _singleton is not None:
        return _singleton

    if (
        not settings.r2_endpoint_url
        or not settings.r2_access_key_id
        or not settings.r2_secret_access_key
    ):
        return None

    _singleton = R2ArtifactStore(
        endpoint_url=settings.r2_endpoint_url,
        bucket=settings.r2_bucket_name,
        access_key_id=settings.r2_access_key_id,
        secret_access_key=settings.r2_secret_access_key,
        region=settings.r2_region,
    )
    return _singleton


async def aclose_artifact_store() -> None:
    """Tear down the singleton (FastAPI shutdown hook)."""
    global _singleton
    if _singleton is not None:
        try:
            await _singleton.aclose()
        finally:
            _singleton = None
