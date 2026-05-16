"""Internal artifact-resolve endpoint (ADR-0069 D2).

The public-facing surface for artifacts is the Cloudflare Worker bound to
``artifacts.frenchforet.com``. The Worker validates Cloudflare Access and
then needs to translate ``{artifact_id}`` -> ``{r2_key, content_type,
size_bytes}`` so it can stream bytes from R2. This module exposes that
translation as a non-Access-gated endpoint that the Worker calls back into
over the existing Cloudflare Tunnel, authenticating with a shared secret.

Security model
--------------
* ``X-Internal-Token`` is constant-time compared against
  ``settings.artifact_resolve_internal_token``. Anything else is 401.
* The Worker forwards the Cloudflare Access-verified email as
  ``X-Authenticated-User-Email``. Missing or unknown email returns 404
  (per ADR-0064 D3 — existence-hiding on auth shape mismatch).
* Cross-user lookups also return 404 — the response shape never confirms
  that an artifact exists outside the caller's tenancy.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.config.settings import get_settings
from personal_agent.service.auth import get_or_create_user_by_email
from personal_agent.service.database import get_db_session
from personal_agent.service.models import ArtifactModel

log = structlog.get_logger(__name__)
settings = get_settings()


router = APIRouter(prefix="/internal/artifacts", tags=["artifacts-internal"])


class ArtifactResolveResponse(BaseModel):
    """Metadata block the Worker needs to serve a single artifact."""

    artifact_id: UUID
    r2_key: str
    content_type: str
    size_bytes: int
    created_at: datetime


def _verify_internal_token(request: Request) -> None:
    """Reject the request unless the Worker's shared secret matches.

    Failure mode is 401 — this is the *only* code path that returns 401;
    every other failure (missing email, unknown artifact, cross-user) maps
    to 404 by design (ADR-0064 D3).
    """
    expected = settings.artifact_resolve_internal_token
    presented = request.headers.get("x-internal-token")
    if not expected or not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


@router.get(
    "/{artifact_id}",
    response_model=ArtifactResolveResponse,
    responses={
        401: {"description": "Internal token missing or invalid."},
        404: {"description": "Artifact not visible to the resolved user."},
    },
)
async def resolve_artifact(
    artifact_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> ArtifactResolveResponse:
    """Translate an artifact id into the R2 metadata the Worker needs."""
    _verify_internal_token(request)

    email = request.headers.get("x-authenticated-user-email")
    if not email:
        # The Worker only forwards this header after Cloudflare Access
        # validates the JWT. Its absence means the call did not come
        # through Access — 404 per ADR-0064 D3.
        log.info("artifact_resolve_missing_email", artifact_id=str(artifact_id))
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    user_id = await get_or_create_user_by_email(db, email)

    # Select individual columns rather than the whole ORM entity so the
    # returned Row carries native scalar types (UUID, str, int, datetime)
    # instead of SQLAlchemy Column wrappers mypy can't unwrap.
    result = await db.execute(
        select(
            ArtifactModel.id,
            ArtifactModel.r2_key,
            ArtifactModel.content_type,
            ArtifactModel.size_bytes,
            ArtifactModel.created_at,
        ).where(
            ArtifactModel.id == artifact_id,
            ArtifactModel.user_id == user_id,
        )
    )
    row = result.one_or_none()
    if row is None:
        log.info(
            "artifact_resolve_not_found",
            artifact_id=str(artifact_id),
            user_id=str(user_id),
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return ArtifactResolveResponse(
        artifact_id=row.id,
        r2_key=row.r2_key,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        created_at=row.created_at,
    )
