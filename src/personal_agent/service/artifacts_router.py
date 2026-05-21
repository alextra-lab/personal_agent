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
from datetime import datetime, timezone
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
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


router = APIRouter(tags=["artifacts"])


class ArtifactResolveResponse(BaseModel):
    """Metadata block the Worker needs to serve a single artifact."""

    artifact_id: UUID
    r2_key: str
    content_type: str
    size_bytes: int
    created_at: datetime


class ArtifactSummary(BaseModel):
    """Public-facing metadata for a single artifact (FRE-368).

    Intentionally omits ``r2_key`` and ``embedding`` — those are
    server-side implementation details that must never reach the client.
    """

    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    public_url: str | None
    slug: str | None
    title: str | None
    summary: str | None
    content_type: str
    size_bytes: int
    tags: list[str]
    created_at: datetime


class ArtifactListResponse(BaseModel):
    """Response body for GET /api/v1/artifacts."""

    model_config = ConfigDict(frozen=True)

    items: list[ArtifactSummary]


def _verify_internal_token(request: Request) -> None:
    """Reject the request unless the Worker's shared secret matches."""
    expected = settings.artifact_resolve_internal_token
    presented = request.headers.get("x-internal-token")
    if not expected or not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


def _artifacts_public_base_url() -> str | None:
    """Return the configured artifacts public base URL, or None."""
    base = settings.artifacts_public_base_url
    return base.rstrip("/") if base else None


async def _resolve_user_via_cf_access(request: Request, db: AsyncSession) -> UUID:
    """Verify the CF Access JWT and return the resolved user_id.

    The JWT is read from the standard header used by the browser-facing PWA
    (``cf-access-jwt-assertion``). Raises HTTP 401 / 503 on failure —
    never falls back to plaintext email headers.
    """
    verifier = get_verifier()
    if verifier is None:
        log.error("artifacts_public_verifier_missing")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    jwt_token = request.headers.get("cf-access-jwt-assertion") or request.headers.get(
        "x-cf-access-jwt-assertion"
    )
    if not jwt_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        claims = await verifier.verify(jwt_token)
    except CFAccessVerifierError as exc:
        log.info(
            "artifacts_public_jwt_invalid",
            error_class=type(exc).__name__,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from exc

    return await get_or_create_user_by_email(db, claims.email)


def _row_to_summary(row: object) -> ArtifactSummary:
    """Convert a SQLAlchemy Row (or SimpleNamespace) to ArtifactSummary."""
    base = _artifacts_public_base_url()
    raw_id = getattr(row, "id", None)
    artifact_id: UUID = raw_id if isinstance(raw_id, UUID) else UUID(str(raw_id))
    public_url = f"{base}/{artifact_id}" if base else None
    return ArtifactSummary(
        artifact_id=artifact_id,
        public_url=public_url,
        slug=getattr(row, "slug", None),
        title=getattr(row, "title", None),
        summary=getattr(row, "summary", None),
        content_type=getattr(row, "content_type", ""),
        size_bytes=getattr(row, "size_bytes", 0),
        tags=list(getattr(row, "tags", []) or []),
        created_at=getattr(row, "created_at", datetime.now(timezone.utc)),
    )


@router.get(
    "/internal/artifacts/{artifact_id}",
    response_model=ArtifactResolveResponse,
    tags=["artifacts-internal"],
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


# ---------------------------------------------------------------------------
# FRE-368 — public CF-Access-gated endpoints for the PWA
# ---------------------------------------------------------------------------


@router.get(
    "/api/v1/artifacts",
    response_model=ArtifactListResponse,
    tags=["artifacts-public"],
    responses={
        401: {"description": "Missing or invalid CF Access JWT."},
        503: {"description": "CF Access verifier not configured."},
    },
)
async def list_artifacts(
    request: Request,
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
    type: str = Query("artifact", pattern=r"^(artifact|note|upload|capture)$"),
    prefix: str | None = Query(None, max_length=64),
    k: int = Query(20, ge=1, le=100),
    since: str | None = Query(None),
) -> ArtifactListResponse:
    """List the authenticated user's artifacts, newest first.

    Authentication: CF Access JWT in ``cf-access-jwt-assertion`` header.
    No ``X-Internal-Token`` is required — this endpoint is for the browser
    (PWA), not the Worker.
    """
    user_id = await _resolve_user_via_cf_access(request, db)

    result = await db.execute(
        text(
            """
            SELECT id, slug, title, summary, content_type, size_bytes,
                   tags, created_at
            FROM artifacts
            WHERE user_id = :user_id
              AND type = :type
              AND (:prefix IS NULL OR slug LIKE :prefix || '%')
              AND (:since IS NULL OR created_at > CAST(:since AS TIMESTAMPTZ))
            ORDER BY created_at DESC
            LIMIT :k
            """
        ),
        {
            "user_id": user_id,
            "type": type,
            "prefix": prefix,
            "since": since,
            "k": k,
        },
    )
    rows = result.all()
    return ArtifactListResponse(items=[_row_to_summary(r) for r in rows])


@router.get(
    "/api/v1/artifacts/{artifact_id}",
    response_model=ArtifactSummary,
    tags=["artifacts-public"],
    responses={
        401: {"description": "Missing or invalid CF Access JWT."},
        404: {"description": "Artifact not found or not owned by this user."},
        503: {"description": "CF Access verifier not configured."},
    },
)
async def get_artifact_metadata(
    artifact_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> ArtifactSummary:
    """Metadata-only fetch for a single artifact.

    Bytes never flow through this endpoint — the browser opens the artifact
    at ``artifacts.frenchforet.com/{artifact_id}`` where the Worker
    streams bytes directly from R2.

    Cross-user access returns 404 (existence-hiding per ADR-0064 D3).
    """
    user_id = await _resolve_user_via_cf_access(request, db)

    result = await db.execute(
        text(
            """
            SELECT id, slug, title, summary, content_type, size_bytes,
                   tags, created_at
            FROM artifacts
            WHERE id = :artifact_id
              AND user_id = :user_id
            """
        ),
        {
            "artifact_id": artifact_id,
            "user_id": user_id,
        },
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return _row_to_summary(row)
