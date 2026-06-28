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
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.config.settings import get_settings
from personal_agent.service.auth import RequestUser, get_or_create_user_by_email, get_request_user
from personal_agent.service.cf_access_jwt import (
    CFAccessVerifierError,
    get_verifier,
)
from personal_agent.service.cf_service_token import cf_access_service_token_headers
from personal_agent.service.database import get_db_session
from personal_agent.service.models import ArtifactModel
from personal_agent.storage import get_artifact_store
from personal_agent.storage.artifact_export import (
    ArtifactExportError,
    AssetFetcher,
    ExportMode,
    SubstitutionMap,
    export_artifact_html,
    load_substitution_map,
)
from personal_agent.telemetry.trace import SystemTraceContext

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
    ctx = SystemTraceContext.new("artifact_resolve")

    verifier = get_verifier()
    if verifier is None:
        # cf_access_team_domain / cf_access_aud not configured. Fail
        # closed — the endpoint cannot enforce its security promise.
        log.error(
            "artifact_resolve_misconfigured",
            artifact_id=str(artifact_id),
            reason="cf_access_team_domain or cf_access_aud unset",
            trace_id=ctx.trace_id,
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    jwt_token = request.headers.get("x-cf-access-jwt-assertion") or request.headers.get(
        "cf-access-jwt-assertion"
    )
    if not jwt_token:
        log.info(
            "artifact_resolve_missing_jwt",
            artifact_id=str(artifact_id),
            trace_id=ctx.trace_id,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        claims = await verifier.verify(jwt_token)
    except CFAccessVerifierError as exc:
        # Opaque 401 — never disclose which verification step failed.
        log.info(
            "artifact_resolve_jwt_invalid",
            artifact_id=str(artifact_id),
            error_class=type(exc).__name__,
            trace_id=ctx.trace_id,
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
            ArtifactModel.upload_pending == False,  # noqa: E712
        )
    )
    row = result.one_or_none()
    if row is None:
        log.info(
            "artifact_resolve_not_found",
            artifact_id=str(artifact_id),
            user_id=str(user_id),
            trace_id=ctx.trace_id,
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
    responses={401: {"description": "Not authenticated via CF Access."}},
)
async def list_artifacts(
    request_user: Annotated[RequestUser, Depends(get_request_user)],  # noqa: B008
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
    type: str = Query("artifact", pattern=r"^(artifact|note|upload|capture)$"),
    prefix: str | None = Query(None, max_length=64),
    k: int = Query(20, ge=1, le=100),
    since: str | None = Query(None),
) -> ArtifactListResponse:
    """List the authenticated user's artifacts, newest first.

    Authentication: same as the rest of the gateway — CF Access injects the
    ``Cf-Access-Authenticated-User-Email`` header which ``get_request_user``
    reads. This matches the ``agent.frenchforet.com`` CF Access app; the
    internal Worker endpoint uses JWT (different AUD) and is unaffected.
    """
    user_id = request_user.user_id

    result = await db.execute(
        text(
            """
            SELECT id, slug, title, summary, content_type, size_bytes,
                   tags, created_at
            FROM artifacts
            WHERE user_id = :user_id
              AND type = :type
              AND upload_pending = FALSE
              AND (CAST(:prefix AS TEXT) IS NULL OR slug LIKE :prefix || '%')
              AND (CAST(:since AS TEXT) IS NULL OR created_at > CAST(:since AS TIMESTAMPTZ))
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
        401: {"description": "Not authenticated via CF Access."},
        404: {"description": "Artifact not found or not owned by this user."},
    },
)
async def get_artifact_metadata(
    artifact_id: UUID,
    request_user: Annotated[RequestUser, Depends(get_request_user)],  # noqa: B008
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> ArtifactSummary:
    """Metadata-only fetch for a single artifact.

    Bytes never flow through this endpoint — the browser opens the artifact
    at ``artifacts.frenchforet.com/{artifact_id}`` where the Worker
    streams bytes directly from R2.

    Cross-user access returns 404 (existence-hiding per ADR-0064 D3).
    """
    user_id = request_user.user_id

    result = await db.execute(
        text(
            """
            SELECT id, slug, title, summary, content_type, size_bytes,
                   tags, created_at
            FROM artifacts
            WHERE id = :artifact_id
              AND user_id = :user_id
              AND upload_pending = FALSE
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


# ---------------------------------------------------------------------------
# FRE-530 — export-to-standalone (ADR-0089 Addendum A5)
# ---------------------------------------------------------------------------

_EXPORT_NOTE = "leaves-sealed-envelope; runs-unsandboxed-when-opened"
_EXPORT_FETCH_TIMEOUT_S = 15.0


class _HttpAssetFetcher:
    """httpx-backed :class:`AssetFetcher` for the export endpoint.

    SSRF mitigation: a request is made **only** to a host on ``allowed_hosts``
    — the artifacts ``/lib/`` origin plus the public-CDN hosts named in the
    *trusted* substitution map. Any other host (a redirect target, or a URL
    smuggled in via artifact content) is refused before the request is issued,
    and redirects are not followed (a 3xx could otherwise relocate the request
    off an allowed host). The CF Access service token is attached **only** for
    the artifacts origin (Access-gated); public CDN hosts are fetched plain. Any
    non-200 is raised as :class:`ArtifactExportError` so the endpoint maps it to
    502.
    """

    def __init__(
        self,
        *,
        origin_host: str,
        allowed_hosts: frozenset[str],
        timeout: float = _EXPORT_FETCH_TIMEOUT_S,
    ) -> None:
        self._origin_host = origin_host.lower()
        self._allowed_hosts = allowed_hosts
        self._timeout = timeout

    async def fetch(self, url: str) -> bytes:
        host = urlparse(url).netloc.lower()
        if host not in self._allowed_hosts:
            raise ArtifactExportError(f"refusing to fetch from disallowed host: {host!r}")
        headers = cf_access_service_token_headers() if host == self._origin_host else {}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ArtifactExportError(f"failed to fetch {url}: {exc}") from exc
        if response.status_code != 200:
            raise ArtifactExportError(f"fetch {url} returned HTTP {response.status_code}")
        return response.content


def _build_asset_fetcher(sub_map: SubstitutionMap) -> AssetFetcher:
    """Build the export asset fetcher with a host allowlist from the trusted map.

    The allowlist is the artifacts origin host plus every public-CDN host named
    in the substitution map — both come from our own config, never from artifact
    content. This is the seam tests override.
    """
    origin_host = urlparse(sub_map.origin).netloc.lower()
    allowed = {origin_host}
    for asset in sub_map.by_lib_path.values():
        if asset.public_cdn_url:
            cdn_host = urlparse(asset.public_cdn_url).netloc.lower()
            if cdn_host:
                allowed.add(cdn_host)
    return _HttpAssetFetcher(origin_host=origin_host, allowed_hosts=frozenset(allowed))


@router.get(
    "/api/v1/artifacts/{artifact_id}/export",
    tags=["artifacts-public"],
    responses={
        200: {"content": {"text/html": {}}, "description": "Standalone HTML attachment."},
        400: {"description": "Artifact is not HTML — only HTML artifacts export."},
        401: {"description": "Not authenticated via CF Access."},
        404: {"description": "Artifact not found or not owned by this user."},
        502: {"description": "An asset could not be fetched / failed SRI verification."},
        503: {"description": "Artifact substrate not configured on this deployment."},
    },
)
async def export_artifact(
    artifact_id: UUID,
    request_user: Annotated[RequestUser, Depends(get_request_user)],  # noqa: B008
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
    mode: ExportMode = Query("inline"),  # noqa: B008
) -> Response:
    """Export a hosted HTML artifact as a portable standalone file (ADR-0089 A5).

    Two modes: ``inline`` (default, offline-portable — fetch the pinned ``/lib/``
    bytes + stylesheet subresources and inline them) and ``substitute`` (rewrite
    ``/lib/`` references to a public-CDN twin + Subresource Integrity, inline
    fallback where there is no CORS-verified twin). The returned file **leaves
    the sealed envelope** — it runs unsandboxed wherever opened — which is
    acceptable because export is user-initiated and the "never bake secrets into
    an artifact" rule (D4) bounds the payload.

    Cross-user access returns 404 (existence-hiding per ADR-0064 D3). Only
    ``text/html`` artifacts export; anything else is 400. A fetch failure or SRI
    mismatch is 502 (the export never ships un-pinned bytes).
    """
    user_id = request_user.user_id
    ctx = SystemTraceContext.new("artifact_export")

    result = await db.execute(
        text(
            """
            SELECT id, slug, content_type, r2_key
            FROM artifacts
            WHERE id = :artifact_id
              AND user_id = :user_id
              AND upload_pending = FALSE
            """
        ),
        {"artifact_id": artifact_id, "user_id": user_id},
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    if not str(row.content_type).lower().startswith("text/html"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="only HTML artifacts can be exported to standalone.",
        )

    store = get_artifact_store()
    if store is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    raw = await store.get(row.r2_key, trace_id=ctx.trace_id)
    html = raw.decode("utf-8", errors="replace")

    sub_map = load_substitution_map()
    fetcher = _build_asset_fetcher(sub_map)
    try:
        out = await export_artifact_html(html=html, mode=mode, sub_map=sub_map, fetcher=fetcher)
    except ArtifactExportError as exc:
        log.warning(
            "artifact_export_failed",
            trace_id=ctx.trace_id,
            user_id=str(user_id),
            artifact_id=str(artifact_id),
            mode=mode,
            error=str(exc),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    filename = f"{row.slug or artifact_id}.html"
    log.info(
        "artifact_export",
        trace_id=ctx.trace_id,
        user_id=str(user_id),
        artifact_id=str(artifact_id),
        mode=mode,
        size_in=len(html),
        size_out=len(out),
    )
    return Response(
        content=out,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Artifact-Export-Mode": mode,
            "X-Artifact-Export-Note": _EXPORT_NOTE,
        },
    )
