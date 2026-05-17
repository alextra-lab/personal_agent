"""Internal artifact-resolve endpoint (ADR-0069 D2).

The public-facing surface for artifacts is the Cloudflare Worker bound to
``artifacts.frenchforet.com``. The Worker validates Cloudflare Access and
then needs to translate ``{artifact_id}`` -> ``{r2_key, content_type,
size_bytes}`` so it can stream bytes from R2. This module exposes that
translation as a non-Access-gated endpoint that the Worker calls back into
over the existing Cloudflare Tunnel.

Security model (post-2026-05-17 hardening)
-------------------------------------------
Two gates, both required. Either failure is 401 / 503 — never 200.

1. ``X-Internal-Token`` is constant-time compared against
   ``settings.artifact_resolve_internal_token``. This is a coarse filter
   that rejects calls that don't originate from our Worker. The token is
   not the identity — only a gatekeeper.
2. ``X-Cf-Access-Jwt-Assertion`` is verified against the Cloudflare Access
   JWKS for the configured team domain and ``aud``. The verified ``email``
   claim is the *only* trusted identity source. Forwarded headers like
   ``X-Authenticated-User-Email`` are ignored — the Worker cannot be
   trusted to filter, only to forward.

Cross-user lookups still return 404 (existence-hiding per ADR-0064 D3).
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
from personal_agent.service.cf_access_jwt import (
    CFAccessVerifierError,
    get_verifier,
)
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
    """Reject the request unless the Worker's shared secret matches."""
    expected = settings.artifact_resolve_internal_token
    presented = request.headers.get("x-internal-token")
    if not expected or not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


@router.get(
    "/{artifact_id}",
    response_model=ArtifactResolveResponse,
    responses={
        401: {"description": "Missing/invalid internal token or CF Access JWT."},
        404: {"description": "Artifact not visible to the resolved user."},
        503: {"description": "CF Access verifier not configured on this deployment."},
    },
)
async def resolve_artifact(
    artifact_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> ArtifactResolveResponse:
    """Translate an artifact id into the R2 metadata the Worker needs.

    Authentication: ``X-Internal-Token`` filters out callers that aren't
    our Worker; ``X-Cf-Access-Jwt-Assertion`` is then cryptographically
    verified to establish the user identity. Identity is *never* taken
    from a plaintext forwarded header.
    """
    _verify_internal_token(request)

    verifier = get_verifier()
    if verifier is None:
        # cf_access_team_domain / cf_access_aud not configured. Fail
        # closed — the endpoint cannot enforce its security promise.
        log.error(
            "artifact_resolve_misconfigured",
            artifact_id=str(artifact_id),
            reason="cf_access_team_domain or cf_access_aud unset",
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    jwt_token = request.headers.get("x-cf-access-jwt-assertion") or request.headers.get(
        "cf-access-jwt-assertion"
    )
    if not jwt_token:
        log.info("artifact_resolve_missing_jwt", artifact_id=str(artifact_id))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        claims = await verifier.verify(jwt_token)
    except CFAccessVerifierError as exc:
        # Opaque 401 — never disclose which verification step failed.
        log.info(
            "artifact_resolve_jwt_invalid",
            artifact_id=str(artifact_id),
            error_class=type(exc).__name__,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from exc

    user_id = await get_or_create_user_by_email(db, claims.email)

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
