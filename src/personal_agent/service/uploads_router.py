"""User-upload presign/complete flow (FRE-369 / ADR-0069).

Two endpoints:
  POST /api/uploads/presign          — validate, insert pending row, return presigned PUT URL
  POST /api/uploads/{artifact_id}/complete — verify R2 HEAD, clear upload_pending

No DELETE endpoint: clients cancel by removing the chip locally; pending rows
expire after 30 minutes via ``expire_pending_uploads`` (called by the lifespan
background task in app.py).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.config import settings
from personal_agent.service.auth import RequestUser, get_request_user
from personal_agent.service.database import get_db_session
from personal_agent.storage import get_artifact_store
from personal_agent.storage.artifact_store import ArtifactStoreError, build_r2_key
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

# Presigned URL lifetime (seconds).
_PRESIGN_EXPIRY_SECONDS: int = 300

# Pending rows older than this are cleaned up by the background task.
_UPLOAD_EXPIRY_MINUTES: int = 30

# Maximum HEAD retries on /complete before giving up.
_HEAD_MAX_ATTEMPTS: int = 2
_HEAD_RETRY_DELAY_SECONDS: float = 0.2

# MIME types accepted for user upload.
ALLOWED_UPLOAD_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/svg+xml",
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
    }
)

# Map MIME type to file extension (used in R2 key construction).
_MIME_TO_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/csv": "csv",
    "application/json": "json",
}


def _get_store() -> Any:
    """Return the process-level R2ArtifactStore (injectable for tests)."""
    return get_artifact_store()


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class PresignRequest(BaseModel):
    """Body for POST /api/uploads/presign."""

    model_config = ConfigDict(frozen=True)

    filename: str
    content_type: str
    size_hint: int


class PresignResponse(BaseModel):
    """Response for POST /api/uploads/presign."""

    model_config = ConfigDict(frozen=True)

    artifact_id: str
    upload_url: str
    expires_in: int


class CompleteResponse(BaseModel):
    """Response for POST /api/uploads/{artifact_id}/complete."""

    model_config = ConfigDict(frozen=True)

    artifact_id: str
    content_type: str
    size_bytes: int
    title: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/presign", response_model=PresignResponse, status_code=200)
async def presign_upload(
    body: PresignRequest,
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> PresignResponse:
    """Validate the upload request, mint a presigned PUT URL, insert a pending row.

    Args:
        body: Filename, content-type, and client-side size hint.
        request_user: Verified caller identity.
        db: Database session.

    Returns:
        Presigned URL, artifact_id, and URL expiry in seconds.

    Raises:
        HTTPException 415: Content-type not in ``ALLOWED_UPLOAD_CONTENT_TYPES``.
        HTTPException 413: ``size_hint`` exceeds ``upload_max_size_bytes``.
        HTTPException 502: R2 presign call failed.
    """
    if body.content_type not in ALLOWED_UPLOAD_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"content_type {body.content_type!r} not allowed for uploads",
        )

    max_size = settings.upload_max_size_bytes
    if body.size_hint > max_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"size_hint {body.size_hint} exceeds maximum {max_size}",
        )

    store = _get_store()
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Artifact store not configured",
        )

    artifact_id = uuid4()
    ext = _MIME_TO_EXT.get(body.content_type, "bin")
    r2_key = build_r2_key(
        type="upload",
        user_id=request_user.user_id,
        session_id=None,
        artifact_id=artifact_id,
        slug=None,
        ext=ext,
    )

    try:
        upload_url = await store.generate_presigned_put_url(
            r2_key=r2_key,
            content_type=body.content_type,
            max_size=max_size,
            expires_in=_PRESIGN_EXPIRY_SECONDS,
        )
    except ArtifactStoreError as exc:
        log.warning("upload_presign_r2_failed", error=str(exc), user_id=str(request_user.user_id))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to generate upload URL",
        ) from exc

    now = datetime.now(tz=timezone.utc)
    await db.execute(
        text(
            """
            INSERT INTO artifacts
                (id, user_id, session_id, type, slug, title, summary,
                 content_type, size_bytes, r2_key, created_by, created_at, upload_pending)
            VALUES
                (:id, :user_id, NULL, 'upload', NULL, :title, NULL,
                 :content_type, 0, :r2_key, 'user', :created_at, TRUE)
            """
        ),
        {
            "id": str(artifact_id),
            "user_id": str(request_user.user_id),
            "title": body.filename,
            "content_type": body.content_type,
            "r2_key": r2_key,
            "created_at": now,
        },
    )
    await db.commit()

    log.info(
        "upload_presigned",
        artifact_id=str(artifact_id),
        content_type=body.content_type,
        user_id=str(request_user.user_id),
    )
    return PresignResponse(
        artifact_id=str(artifact_id),
        upload_url=upload_url,
        expires_in=_PRESIGN_EXPIRY_SECONDS,
    )


@router.post("/{artifact_id}/complete", response_model=CompleteResponse, status_code=200)
async def complete_upload(
    artifact_id: UUID,
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> CompleteResponse:
    """Verify the R2 object exists, validate size, and clear upload_pending.

    Args:
        artifact_id: The UUID returned by the presign endpoint.
        request_user: Verified caller identity (must own the pending row).
        db: Database session.

    Returns:
        Completed artifact metadata.

    Raises:
        HTTPException 404: Row not found, not owned by caller, or already completed.
        HTTPException 413: Actual object size exceeds ``upload_max_size_bytes``.
        HTTPException 502: R2 HEAD failed after retries.
    """
    result = await db.execute(
        text(
            """
            SELECT id, user_id, r2_key, content_type, title
            FROM artifacts
            WHERE id = :id
              AND user_id = :user_id
              AND upload_pending = TRUE
            """
        ),
        {"id": str(artifact_id), "user_id": str(request_user.user_id)},
    )
    row = result.first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Upload not found or already completed",
        )

    store = _get_store()
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Artifact store not configured",
        )

    # HEAD R2 with bounded retry to tolerate slight propagation lag.
    head: dict[str, Any] | None = None
    last_exc: ArtifactStoreError | None = None
    for attempt in range(_HEAD_MAX_ATTEMPTS):
        try:
            head = await store.head(r2_key=row.r2_key)
            break
        except ArtifactStoreError as exc:
            last_exc = exc
            if attempt < _HEAD_MAX_ATTEMPTS - 1:
                await asyncio.sleep(_HEAD_RETRY_DELAY_SECONDS)

    if head is None:
        log.warning(
            "upload_complete_r2_head_failed",
            artifact_id=str(artifact_id),
            error=str(last_exc),
            user_id=str(request_user.user_id),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Upload not yet available in object store",
        )

    actual_size: int = int(head.get("content_length", 0))
    max_size = settings.upload_max_size_bytes
    if actual_size > max_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Uploaded file size {actual_size} exceeds maximum {max_size}",
        )

    await db.execute(
        text(
            """
            UPDATE artifacts
               SET upload_pending = FALSE,
                   size_bytes = :size
             WHERE id = :id
               AND user_id = :user_id
            """
        ),
        {"id": str(artifact_id), "user_id": str(request_user.user_id), "size": actual_size},
    )
    await db.commit()

    log.info(
        "upload_completed",
        artifact_id=str(artifact_id),
        size_bytes=actual_size,
        user_id=str(request_user.user_id),
    )
    return CompleteResponse(
        artifact_id=str(artifact_id),
        content_type=row.content_type,
        size_bytes=actual_size,
        title=row.title or str(artifact_id),
    )


# ---------------------------------------------------------------------------
# Background expiry
# ---------------------------------------------------------------------------


async def expire_pending_uploads(db_factory: Any) -> int:
    """Delete pending upload rows older than ``_UPLOAD_EXPIRY_MINUTES`` minutes.

    Args:
        db_factory: Callable that returns an async context manager yielding
            an ``AsyncSession`` (typically ``AsyncSessionLocal``).

    Returns:
        Number of rows deleted.
    """
    async with db_factory() as db:
        result = await db.execute(
            text(
                f"""
                DELETE FROM artifacts
                 WHERE upload_pending = TRUE
                   AND created_at < NOW() - INTERVAL '{_UPLOAD_EXPIRY_MINUTES} minutes'
                """
            )
        )
        await db.commit()
        return getattr(result, "rowcount", 0)
